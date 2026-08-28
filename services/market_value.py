"""
Market-value benchmark for a property, derived from RentCast's Value
Estimate (AVM) response (integrations.rentcast.RentCastClient.get_value_estimate).

RentCast doesn't return an explicit confidence label, so confidence here is
derived: "high" only when the AVM produced a price backed by enough comps
with a tight price range; "low" in every other case, including whenever
the comps-median fallback below is used instead of the AVM price directly.
"""

from dataclasses import dataclass
from statistics import median

# AVM confidence is "high" only when RentCast's price range is within this
# fraction of the price itself (e.g. 0.20 = the range spans at most 20% of
# the price). This is a judgment call, not a value RentCast provides
# directly — tune if real-world results suggest a different cutoff.
TIGHT_RANGE_RATIO = 0.20

# Minimum comps behind the AVM price required for "high" confidence,
# regardless of how tight the price range is.
MIN_COMPS_FOR_HIGH_CONFIDENCE = 3


class MarketValueUnavailableError(Exception):
    """
    Neither RentCast's AVM price nor a comps-median fallback could be
    computed for this property — no usable price, and no comps with both
    a price and squareFootage to fall back on.
    """


@dataclass
class MarketValueEstimate:
    market_value_estimate: float
    market_value_method: str  # 'rentcast_avm' | 'comps_median_sqft'
    market_value_confidence: str  # 'high' | 'low'
    market_value_comps_count: int


def estimate_market_value(avm_json):
    """
    avm_json: a RentCast Value Estimate (AVM) response dict, as returned by
    RentCastClient.get_value_estimate() / lookup_property().

    Raises MarketValueUnavailableError if neither the AVM price nor a
    comps-median fallback can be computed.
    """
    comps = avm_json.get('comparables') or []
    comps_count = len(comps)
    price = avm_json.get('price')

    if price is not None:
        return MarketValueEstimate(
            market_value_estimate=price,
            market_value_method='rentcast_avm',
            market_value_confidence=_avm_confidence(avm_json, comps_count),
            market_value_comps_count=comps_count,
        )

    return _comps_median_fallback(avm_json, comps)


def _avm_confidence(avm_json, comps_count):
    if comps_count < MIN_COMPS_FOR_HIGH_CONFIDENCE:
        return 'low'

    price = avm_json.get('price')
    price_low = avm_json.get('priceRangeLow')
    price_high = avm_json.get('priceRangeHigh')
    if not price or price_low is None or price_high is None:
        return 'low'

    range_ratio = (price_high - price_low) / price
    return 'high' if range_ratio <= TIGHT_RANGE_RATIO else 'low'


def _comps_median_fallback(avm_json, comps):
    subject_sqft = (avm_json.get('subjectProperty') or {}).get('squareFootage')

    price_per_sqft_values = [
        comp['price'] / comp['squareFootage']
        for comp in comps
        if comp.get('price') and comp.get('squareFootage')
    ]

    if not subject_sqft or not price_per_sqft_values:
        raise MarketValueUnavailableError(
            'No AVM price, and no comps with both price and squareFootage to fall back on'
        )

    estimate = median(price_per_sqft_values) * subject_sqft

    return MarketValueEstimate(
        market_value_estimate=estimate,
        market_value_method='comps_median_sqft',
        market_value_confidence='low',
        market_value_comps_count=len(comps),
    )
