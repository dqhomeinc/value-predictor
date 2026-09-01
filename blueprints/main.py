import logging
import os

from flask import Blueprint, flash, jsonify, redirect, render_template, request, url_for
from flask_login import current_user, login_required

from integrations.rentcast import RentCastError
from models import Analysis
from services.analyzer import AnalysisError, build_rentcast_client, run_analysis
from services.market_value import MarketValueUnavailableError

main_bp = Blueprint('main', __name__)
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
    # RENTCAST_MOCK=1 (see services.analyzer.build_rentcast_client) serves
    # synthetic data without ever needing a real key.
    if not api_key and os.environ.get('RENTCAST_MOCK') != '1':
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
    return render_template('analysis_results.html', analysis=analysis)
