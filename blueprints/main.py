import logging
import os

from flask import Blueprint, flash, jsonify, redirect, render_template, request, url_for
from flask_login import current_user, login_required

from integrations.municipal_zoning import SUPPORTED_JURISDICTIONS, safe_external_url
from integrations.rentcast import RentCastError
from models import Analysis, ChatMessage, db
from services.analyzer import (
    AnalysisError,
    build_municipal_zoning_lookup,
    build_rentcast_client,
    rentcast_mock_enabled,
    run_analysis,
)
from services.dimensional_standards import standards_for
from services.market_value import MarketValueUnavailableError
from services.zoning_chat import (
    MAX_QUESTION_CHARS,
    ChatUnavailableError,
    ask,
    build_chat_client,
    chat_configured,
    chat_mock_enabled,
    daily_message_limit,
    messages_sent_in_last_day,
)
from services.zoning_guidance import GENERIC_NOTE, annotate

main_bp = Blueprint('main', __name__)


@main_bp.app_template_filter('safe_url')
def _safe_url(value):
    """
    Render-time scheme check for links that came from third-party GIS
    attributes. Ingestion already filters these, but zoning detail is
    cached indefinitely, so rows stored before that existed would
    otherwise keep serving whatever they captured. See
    integrations.municipal_zoning.safe_external_url.
    """
    return safe_external_url(value)
logger = logging.getLogger(__name__)

# Failures outside our control (RentCast down/no data for this address,
# too few comps to value it) — degrade to a flashed message and back to
# the form, rather than a 500.
ANALYSIS_FAILURE_ERRORS = (RentCastError, MarketValueUnavailableError, AnalysisError)


@main_bp.route('/health')
def health():
    return jsonify(status='ok')


@main_bp.route('/')
@login_required
def index():
    return render_template('index.html')


def _parse_required_float(raw_value, field_label, *, allow_negative=False):
    try:
        value = float(raw_value)
    except (TypeError, ValueError):
        raise ValueError(f'{field_label} must be a number.') from None
    if not allow_negative and value < 0:
        raise ValueError(f'{field_label} cannot be negative.')
    return value


@main_bp.route('/analyses', methods=['GET', 'POST'])
@login_required
def analyses():
    if request.method == 'GET':
        past_analyses = (
            Analysis.query.filter_by(user_id=current_user.id)
            .order_by(Analysis.created_at.desc())
            .all()
        )
        return render_template('analyses_list.html', analyses=past_analyses)

    address = request.form.get('address', '').strip()
    form_values = {
        'address': address,
        'purchase_price': request.form.get('purchase_price', ''),
        'cost_per_sqft': request.form.get('cost_per_sqft', ''),
        'profit_margin_pct': request.form.get('profit_margin_pct', ''),
    }

    if not address:
        flash('Address is required.', 'error')
        return render_template('index.html', form_values=form_values), 400

    try:
        purchase_price = _parse_required_float(form_values['purchase_price'], 'Purchase price')
        cost_per_sqft = _parse_required_float(form_values['cost_per_sqft'], 'Cost per sq ft')
        # A target margin of 0 or below is unusual but not invalid input —
        # the calculator itself handles it fine.
        profit_margin_pct = _parse_required_float(
            form_values['profit_margin_pct'], 'Target profit margin', allow_negative=True
        )
    except ValueError as exc:
        flash(str(exc), 'error')
        return render_template('index.html', form_values=form_values), 400

    api_key = os.environ.get('RENTCAST_API_KEY')
    # Mock mode (see services.analyzer.rentcast_mock_enabled, on by default
    # for local dev) serves synthetic data without ever needing a real key.
    if not api_key and not rentcast_mock_enabled():
        logger.error('RENTCAST_API_KEY is not set — cannot run analysis')
        flash('Property data is temporarily unavailable. Please try again later.', 'error')
        return render_template('index.html', form_values=form_values), 503

    # Set from the results page's "Refresh with full data" button, to
    # upgrade a comp_cached analysis (see services/analyzer.py) to a real
    # lookup — spends 2 real RentCast calls where a normal submission of
    # an address seen before as a comp would otherwise spend 0.
    force_refresh = request.form.get('force_refresh') == '1'

    client = build_rentcast_client(api_key)
    try:
        analysis = run_analysis(
            user=current_user,
            address=address,
            purchase_price=purchase_price,
            cost_per_sqft=cost_per_sqft,
            profit_margin_pct=profit_margin_pct,
            rentcast_client=client,
            force_refresh=force_refresh,
            municipal_zoning_lookup=build_municipal_zoning_lookup(),
        )
    except ANALYSIS_FAILURE_ERRORS as exc:
        logger.warning('Analysis failed for %r: %s', address, exc)
        flash("We couldn't find enough property data for that address. Please try again.", 'error')
        return render_template('index.html', form_values=form_values), 502

    return redirect(url_for('main.analysis_detail', analysis_id=analysis.id))


@main_bp.route('/analyses/<int:analysis_id>')
@login_required
def analysis_detail(analysis_id):
    analysis = Analysis.query.filter_by(id=analysis_id, user_id=current_user.id).first_or_404()
    # Turn the stored restriction labels into "what this does to a rebuild"
    # guidance at render time rather than storing it — the explanations are
    # editorial and should improve for past analyses too, not be frozen
    # into whatever text shipped the day they were run.
    restrictions = annotate((analysis.zoning_detail or {}).get('restrictions'))
    return render_template(
        'analysis_results.html',
        analysis=analysis,
        restrictions=restrictions,
        zoning_note=GENERIC_NOTE,
        supported_jurisdictions=SUPPORTED_JURISDICTIONS,
        standards=standards_for(analysis.zoning_detail, analysis.property_lot_size),
        chat_messages=analysis.chat_messages,
        chat_available=chat_configured(),
        chat_mock=chat_mock_enabled(),
        chat_max_chars=MAX_QUESTION_CHARS,
    )


@main_bp.route('/analyses/<int:analysis_id>/chat', methods=['POST'])
@login_required
def analysis_chat(analysis_id):
    """
    One question to the build-restrictions chat (services/zoning_chat.py),
    answered as JSON: {'reply', 'sources'} or {'error'}. The question and
    reply are saved together, and only once the reply succeeds.
    """
    analysis = Analysis.query.filter_by(id=analysis_id, user_id=current_user.id).first_or_404()
    payload = request.get_json(silent=True)
    question = str((payload if isinstance(payload, dict) else {}).get('message') or '').strip()
    if not question:
        return jsonify(error='Type a question first.'), 400
    if len(question) > MAX_QUESTION_CHARS:
        return jsonify(error=f'Questions are limited to {MAX_QUESTION_CHARS:,} characters.'), 400

    client = build_chat_client()
    if client is None:
        return jsonify(error="The chat isn't set up on this server yet."), 503
    limit = daily_message_limit()
    if messages_sent_in_last_day(current_user.id) >= limit:
        return jsonify(error=f"You've reached the limit of {limit} questions a day. Try again later."), 429

    try:
        reply = ask(client, analysis, analysis.chat_messages, question)
    except ChatUnavailableError as exc:
        logger.warning('Chat failed for analysis %s: %s', analysis.id, exc)
        return jsonify(error="Couldn't get an answer right now. Please try again in a moment."), 502

    db.session.add_all([
        ChatMessage(analysis_id=analysis.id, role='user', content=question),
        ChatMessage(analysis_id=analysis.id, role='assistant', content=reply.text,
                    sources=reply.sources, web_searches=reply.web_searches),
    ])
    db.session.commit()
    return jsonify(reply=reply.text, sources=reply.sources)
