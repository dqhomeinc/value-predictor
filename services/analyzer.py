"""
Orchestrates one "analyze a property" request: RentCast lookup -> market
value benchmark -> rebuild deal math -> persist. Single entry point for
both the web route and future tests, so nothing upstream needs to know
about RentCast, market value math, or deal math individually.
"""

import logging

from integrations.rentcast import RentCastClient
from models import Analysis, db
from services.market_value import estimate_market_value
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


def run_analysis(user, address, purchase_price, cost_per_sqft, profit_margin_pct, rentcast_client):
    """
    profit_margin_pct is a whole-number percentage (20 for a 20% target
    margin) — the human-friendly unit used everywhere outside
    rebuild_calc.py, which wants a fraction (0.20).

    Raises integrations.rentcast.RentCastError,
    services.market_value.MarketValueUnavailableError, or AnalysisError on
    failure. Does not catch any of them — that's the caller's job.
    """
    avm_json, property_json, from_cache = rentcast_client.lookup_property(address)
    logger.info('Analysis for %r: RentCast data %s', address, 'from cache' if from_cache else 'freshly fetched')

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
    return RentCastClient(api_key=api_key)
