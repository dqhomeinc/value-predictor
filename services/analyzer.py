"""
Orchestrates one "analyze a property" request: RentCast lookup -> market
value benchmark -> rebuild deal math -> persist. Single entry point for
both the web route and future tests, so nothing upstream needs to know
about RentCast, market value math, or deal math individually.
"""

import logging
import os

from integrations.rentcast import RentCastClient
from models import Analysis, db
from services.market_value import MarketValueEstimate, MarketValueUnavailableError, estimate_market_value
from services.rebuild_calc import calculate_rebuild_deal

logger = logging.getLogger(__name__)


class AnalysisError(Exception):
    """
    The pipeline couldn't produce a usable analysis for this address —
    e.g. RentCast returned a property record with no square footage.
    Distinct from RentCastError/MarketValueUnavailableError (which the
    caller should also handle) so all three can be caught together as
    "analysis failed, degrade gracefully" at the route level.
    """


def run_analysis(user, address, purchase_price, cost_per_sqft, profit_margin_pct, rentcast_client, force_refresh=False):
    """
    profit_margin_pct is a whole-number percentage (20 for a 20% target
    margin) — the human-friendly unit used everywhere outside
    rebuild_calc.py, which wants a fraction (0.20).

    force_refresh forces 2 real RentCast calls even if this address only
    has a 'comp_seed' (partial, pre-seeded-for-free from another address's
    comps) cache entry — see integrations/rentcast.py's lookup_property.

    Raises integrations.rentcast.RentCastError,
    services.market_value.MarketValueUnavailableError, or AnalysisError on
    failure. Does not catch any of them — that's the caller's job.
    """
    avm_json, property_json, from_cache, source = rentcast_client.lookup_property(address, force_refresh=force_refresh)
    logger.info(
        'Analysis for %r: RentCast data %s (source=%s)',
        address, 'from cache' if from_cache else 'freshly fetched', source,
    )

    if source == 'comp_seed':
        # Pre-seeded for free from another address's comps — no zoning/
        # subdivision/history, no independent AVM computation. Use the
        # comp's own sale/listing price directly, explicitly labeled
        # low-confidence, rather than running it through
        # estimate_market_value() (which would just see 0 comps and no
        # way to compute a fallback either).
        price = avm_json.get('price')
        if price is None:
            raise MarketValueUnavailableError(
                f'Comp-cached data for {address!r} has no price — try again with force_refresh'
            )
        market_value = MarketValueEstimate(
            market_value_estimate=price,
            market_value_method='comp_cached',
            market_value_confidence='low',
            market_value_comps_count=0,
        )
    else:
        market_value = estimate_market_value(avm_json)

    subject = avm_json.get('subjectProperty') or {}
    property_sqft = subject.get('squareFootage')
    if not property_sqft:
        raise AnalysisError(f'RentCast returned no square footage for {address!r} — cannot compute build cost')

    deal = calculate_rebuild_deal(
        purchase_price=purchase_price,
        property_sqft=property_sqft,
        cost_per_sqft=cost_per_sqft,
        profit_margin=profit_margin_pct / 100,
        market_value_estimate=market_value.market_value_estimate,
    )

    analysis = Analysis(
        user_id=user.id,
        address=address,
        purchase_price=purchase_price,
        initial_cost_per_sqft=cost_per_sqft,
        initial_profit_margin_pct=profit_margin_pct,
        property_sqft=property_sqft,
        property_lot_size=subject.get('lotSize'),
        property_bedrooms=subject.get('bedrooms'),
        property_bathrooms=subject.get('bathrooms'),
        property_year_built=subject.get('yearBuilt'),
        property_zoning=property_json.get('zoning'),
        property_subdivision=property_json.get('subdivision'),
        property_sale_history=property_json.get('history'),
        property_latitude=subject.get('latitude'),
        property_longitude=subject.get('longitude'),
        market_value_estimate=market_value.market_value_estimate,
        market_value_method=market_value.market_value_method,
        market_value_confidence=market_value.market_value_confidence,
        market_value_comps_count=market_value.market_value_comps_count,
        market_value_comps_snapshot=avm_json.get('comparables'),
        build_cost_estimate=deal.build_cost,
        total_cost_estimate=deal.total_cost,
        required_sale_price=deal.required_sale_price,
        achievable_margin_pct=deal.achievable_margin * 100,
        is_worth_it=deal.is_worth_it,
    )
    db.session.add(analysis)
    db.session.commit()

    return analysis


def build_rentcast_client(api_key):
    """
    RENTCAST_MOCK=1 swaps in synthetic, made-up property data (see
    integrations/rentcast_mock.py) instead of real RentCast calls — for
    burning through the free-tier ~50 calls/month during local dev/manual
    testing. Refuses to activate wherever DATABASE_URL is set, the same
    signal app.create_app() uses to detect a deployed environment (Render)
    — this must never be what a real user's analysis is computed from.
    """
    if os.environ.get('RENTCAST_MOCK') == '1':
        if os.environ.get('DATABASE_URL'):
            raise RuntimeError(
                'RENTCAST_MOCK=1 is set in what looks like a deployed environment '
                '(DATABASE_URL is set) — refusing to serve synthetic property data. '
                'Unset RENTCAST_MOCK or DATABASE_URL.'
            )
        logger.warning('RENTCAST_MOCK=1 — serving synthetic property data, no real RentCast calls will be made')
        from integrations.rentcast_mock import MockRentCastSession
        return RentCastClient(api_key=api_key or 'mock-mode', session=MockRentCastSession())

    return RentCastClient(api_key=api_key)
