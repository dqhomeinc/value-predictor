import pytest

from services.market_value import (
    MarketValueUnavailableError,
    estimate_market_value,
)


def make_avm_json(price=None, price_low=None, price_high=None, comparables=None, subject_sqft=1878):
    return {
        'price': price,
        'priceRangeLow': price_low,
        'priceRangeHigh': price_high,
        'subjectProperty': {'squareFootage': subject_sqft},
        'comparables': comparables or [],
    }


def make_comps(count, price=250_000, sqft=1878):
    return [{'price': price, 'squareFootage': sqft} for _ in range(count)]


class TestAvmPath:
    def test_tight_range_with_enough_comps_is_high_confidence(self):
        avm_json = make_avm_json(
            price=250_000,
            price_low=240_000,  # 40k range / 250k price = 16% -> tight
            price_high=280_000,
            comparables=make_comps(4),
        )
        result = estimate_market_value(avm_json)
        assert result.market_value_estimate == 250_000
        assert result.market_value_method == 'rentcast_avm'
        assert result.market_value_confidence == 'high'
        assert result.market_value_comps_count == 4

    def test_wide_range_is_low_confidence_despite_enough_comps(self):
        avm_json = make_avm_json(
            price=250_000,
            price_low=150_000,  # 200k range / 250k price = 80% -> wide
            price_high=350_000,
            comparables=make_comps(5),
        )
        result = estimate_market_value(avm_json)
        assert result.market_value_method == 'rentcast_avm'
        assert result.market_value_confidence == 'low'

    def test_too_few_comps_is_low_confidence_despite_tight_range(self):
        avm_json = make_avm_json(
            price=250_000,
            price_low=245_000,
            price_high=255_000,
            comparables=make_comps(2),  # below MIN_COMPS_FOR_HIGH_CONFIDENCE
        )
        result = estimate_market_value(avm_json)
        assert result.market_value_confidence == 'low'

    def test_missing_price_range_is_low_confidence(self):
        avm_json = make_avm_json(price=250_000, comparables=make_comps(5))
        result = estimate_market_value(avm_json)
        assert result.market_value_confidence == 'low'

    def test_exactly_at_tight_range_threshold_is_high_confidence(self):
        # Range is exactly 20% of price -> boundary should count as tight (<=).
        avm_json = make_avm_json(
            price=250_000,
            price_low=225_000,
            price_high=275_000,  # 50k / 250k = 0.20 exactly
            comparables=make_comps(3),
        )
        result = estimate_market_value(avm_json)
        assert result.market_value_confidence == 'high'


class TestCompsMedianFallback:
    def test_falls_back_when_price_missing(self):
        avm_json = make_avm_json(
            price=None,
            comparables=[
                {'price': 240_000, 'squareFootage': 1800},  # $133.33/sqft
                {'price': 260_000, 'squareFootage': 2000},  # $130.00/sqft
                {'price': 280_000, 'squareFootage': 2100},  # $133.33/sqft
            ],
            subject_sqft=1878,
        )
        result = estimate_market_value(avm_json)
        assert result.market_value_method == 'comps_median_sqft'
        assert result.market_value_confidence == 'low'
        # Sorted $/sqft: [130.00, 133.33, 133.33] -> median is 133.33 (400/3)
        assert result.market_value_estimate == pytest.approx((400 / 3) * 1878)
        assert result.market_value_comps_count == 3

    def test_skips_comps_missing_price_or_sqft(self):
        avm_json = make_avm_json(
            price=None,
            comparables=[
                {'price': 260_000, 'squareFootage': 2000},  # usable: $130/sqft
                {'price': 300_000},  # missing squareFootage -> skipped
                {'squareFootage': 1900},  # missing price -> skipped
            ],
            subject_sqft=1878,
        )
        result = estimate_market_value(avm_json)
        # Only one usable comp -> median is just that comp's $/sqft.
        assert result.market_value_estimate == pytest.approx(130.0 * 1878)
        # comps_count reflects all comps RentCast returned, not just usable ones.
        assert result.market_value_comps_count == 3

    def test_raises_when_no_price_and_no_usable_comps(self):
        avm_json = make_avm_json(price=None, comparables=[{'price': 200_000}])  # no squareFootage anywhere usable
        with pytest.raises(MarketValueUnavailableError):
            estimate_market_value(avm_json)

    def test_raises_when_no_price_and_no_comps_at_all(self):
        avm_json = make_avm_json(price=None, comparables=[])
        with pytest.raises(MarketValueUnavailableError):
            estimate_market_value(avm_json)

    def test_raises_when_subject_sqft_missing(self):
        avm_json = make_avm_json(
            price=None,
            comparables=make_comps(3),
            subject_sqft=None,
        )
        with pytest.raises(MarketValueUnavailableError):
            estimate_market_value(avm_json)
