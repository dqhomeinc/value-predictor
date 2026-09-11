"""
Orchestrates one "analyze a property" request: RentCast lookup -> market
value benchmark -> rebuild deal math -> persist. Single entry point for
both the web route and future tests, so nothing upstream needs to know
about RentCast, market value math, or deal math individually.
"""

import logging
import os

from integrations.municipal_zoning import (
    DETAIL_VERSION,
    MunicipalZoningUnavailableError,
    lookup_municipal_zoning,
)
from integrations.rentcast import RentCastClient, normalize_address
from models import Analysis, PropertyLookupCache, db
from services.market_value import (
    MarketValueEstimate,
    MarketValueUnavailableError,
    estimate_market_value,
)
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


def run_analysis(
    user, address, purchase_price, cost_per_sqft, profit_margin_pct, rentcast_client,
    force_refresh=False, municipal_zoning_lookup=lookup_municipal_zoning,
):
    """
    profit_margin_pct is a whole-number percentage (20 for a 20% target
    margin) — the human-friendly unit used everywhere outside
    rebuild_calc.py, which wants a fraction (0.20).

    force_refresh forces 2 real RentCast calls even if this address only
    has a 'comp_seed' (partial, pre-seeded-for-free from another address's
    comps) cache entry — see integrations/rentcast.py's lookup_property.

    municipal_zoning_lookup is injectable for tests; production callers
    should leave it at the default (integrations.municipal_zoning's real
    lookup).

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

    zoning, zoning_source, zoning_detail = _resolve_zoning(
        address, property_json.get('zoning'), municipal_zoning_lookup
    )

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
        property_zoning=zoning,
        zoning_source=zoning_source,
        zoning_detail=zoning_detail,
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


def _resolve_zoning(address, rentcast_zoning, municipal_zoning_lookup):
    """
    Zoning plus the parcel's build restrictions, preferring the
    jurisdiction's own GIS over RentCast wherever one is registered for
    this address (see integrations/municipal_zoning.py).

    The municipal lookup is attempted even when RentCast *did* return a
    zoning string, because the two aren't interchangeable: RentCast gives
    a bare base code, while the GIS gives the full code with its suffixes
    plus the overlays and historic designations that decide whether a
    teardown is even permitted. Gating this on RentCast coming back empty
    would hide that from most addresses. Most jurisdictions have no
    adapter, so "no answer" stays the common, expected outcome — RentCast's
    value is then used unchanged.

    Cached on the same PropertyLookupCache row RentCast's own data lives
    on, keyed by the same normalized address, so a jurisdiction's GIS is
    queried once per address, ever — not once per analysis.

    Returns (zoning_code_or_None, source_or_None, detail_dict_or_None).
    """
    cached = PropertyLookupCache.query.filter_by(normalized_address=normalize_address(address)).first()
    detail = cached.municipal_zoning_detail if cached is not None else None
    # Only detail stored at the current shape counts as a cache hit. Older
    # detail lacks whatever's been added to it since and would otherwise be
    # served forever, so it's looked up again. These lookups are free.
    if isinstance(detail, dict) and detail.get('detail_version') == DETAIL_VERSION and cached.municipal_zoning_code:
        return cached.municipal_zoning_code, cached.municipal_zoning_source, detail

    try:
        result = municipal_zoning_lookup(address)
    except MunicipalZoningUnavailableError as exc:
        logger.info('No municipal zoning for %r: %s', address, exc)
        # Fall back to whatever RentCast had, if anything.
        return (rentcast_zoning, 'rentcast', None) if rentcast_zoning else (None, None, None)

    detail = result.as_dict()
    if cached is not None:
        cached.municipal_zoning_code = result.zoning_code
        cached.municipal_zoning_source = result.source
        cached.municipal_zoning_detail = detail
        db.session.commit()

    return result.zoning_code, result.source, detail


def build_municipal_zoning_lookup():
    """
    The real jurisdiction GIS lookup, or a synthetic stand-in under the
    same RENTCAST_MOCK switch that governs RentCast (see
    rentcast_mock_enabled).

    Unlike RentCast there's no quota to protect — these GIS services are
    free and unmetered. Mocking them is about keeping local dev offline
    and deterministic, and about not leaning on a city's servers for
    routine clicking around. The production guard lives in
    build_rentcast_client(), which the same request path always calls
    first and which refuses mock mode wherever DATABASE_URL is set.
    """
    if rentcast_mock_enabled():
        from integrations.municipal_zoning_mock import mock_lookup_municipal_zoning
        return mock_lookup_municipal_zoning

    return lookup_municipal_zoning


def rentcast_mock_enabled():
    """
    Whether to serve synthetic property data — both RentCast's (see
    integrations/rentcast_mock.py) and the municipal zoning GIS lookups
    (integrations/municipal_zoning_mock.py) — instead of real external
    calls. Defaults ON for local dev (no DATABASE_URL set, the same signal
    app.create_app() uses to detect a deployed environment) so mock mode
    doesn't require remembering to opt in, RentCast's free-tier quota
    (~50 calls/month) isn't burned by routine manual testing, and local
    dev works offline without leaning on a city's GIS servers.

    An explicit RENTCAST_MOCK always wins over the default:
      RENTCAST_MOCK=0 — opt out locally, e.g. to verify against real
      RentCast data before a PR.
      RENTCAST_MOCK=1 — force mock on; still refused by
      build_rentcast_client() wherever DATABASE_URL is set (see there).
    """
    mock_env = os.environ.get('RENTCAST_MOCK')
    if mock_env is None:
        return not os.environ.get('DATABASE_URL')
    return mock_env == '1'


def build_rentcast_client(api_key):
    """
    See rentcast_mock_enabled() for when mock mode applies. Refuses to
    activate wherever DATABASE_URL is set, the same signal app.create_app()
    uses to detect a deployed environment (Render) — this must never be
    what a real user's analysis is computed from, whatever RENTCAST_MOCK
    is explicitly set to.
    """
    if rentcast_mock_enabled():
        if os.environ.get('DATABASE_URL'):
            raise RuntimeError(
                'RENTCAST_MOCK is enabled in what looks like a deployed environment '
                '(DATABASE_URL is set) — refusing to serve synthetic property data. '
                'Unset RENTCAST_MOCK or DATABASE_URL.'
            )
        logger.warning('Using synthetic property data (RENTCAST_MOCK) — no real RentCast calls will be made')
        from integrations.rentcast_mock import MockRentCastSession
        return RentCastClient(api_key=api_key or 'mock-mode', session=MockRentCastSession())

    return RentCastClient(api_key=api_key)
