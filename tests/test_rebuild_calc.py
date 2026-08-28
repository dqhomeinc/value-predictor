from services.rebuild_calc import calculate_rebuild_deal


class TestCalculateRebuildDeal:
    def test_normal_case(self):
        # $200k lot, 2000 sqft rebuild at $150/sqft = $300k build cost.
        # Total cost $500k, 20% target margin -> required sale $600k.
        # Market says $650k -> comfortably worth it.
        deal = calculate_rebuild_deal(
            purchase_price=200_000,
            property_sqft=2000,
            cost_per_sqft=150,
            profit_margin=0.20,
            market_value_estimate=650_000,
        )
        assert deal.build_cost == 300_000
        assert deal.total_cost == 500_000
        assert deal.required_sale_price == 600_000
        assert deal.achievable_margin == 0.30  # (650k - 500k) / 500k
        assert deal.is_worth_it is True

    def test_zero_margin_required_sale_equals_total_cost(self):
        deal = calculate_rebuild_deal(
            purchase_price=200_000,
            property_sqft=2000,
            cost_per_sqft=150,
            profit_margin=0.0,
            market_value_estimate=500_000,
        )
        assert deal.required_sale_price == deal.total_cost == 500_000
        assert deal.is_worth_it is True  # market value == required sale price

    def test_negative_margin_not_worth_it(self):
        # Market value comes in below total cost -> already underwater
        # before even applying a target margin.
        deal = calculate_rebuild_deal(
            purchase_price=200_000,
            property_sqft=2000,
            cost_per_sqft=150,
            profit_margin=0.20,
            market_value_estimate=400_000,
        )
        assert deal.achievable_margin == -0.20  # (400k - 500k) / 500k
        assert deal.is_worth_it is False

    def test_zero_build_cost(self):
        # cost_per_sqft of 0 (e.g. UI default before the user sets a real
        # value) -> total_cost collapses to just the purchase price.
        deal = calculate_rebuild_deal(
            purchase_price=300_000,
            property_sqft=2000,
            cost_per_sqft=0,
            profit_margin=0.10,
            market_value_estimate=350_000,
        )
        assert deal.build_cost == 0
        assert deal.total_cost == 300_000
        assert deal.required_sale_price == 330_000
        assert deal.is_worth_it is True

    def test_zero_total_cost_does_not_divide_by_zero(self):
        deal = calculate_rebuild_deal(
            purchase_price=0,
            property_sqft=2000,
            cost_per_sqft=0,
            profit_margin=0.10,
            market_value_estimate=100_000,
        )
        assert deal.total_cost == 0
        assert deal.achievable_margin == 0.0
        assert deal.required_sale_price == 0
        assert deal.is_worth_it is True  # market value >= 0

    def test_boundary_market_value_equals_required_sale_price(self):
        # Exact equality should count as worth it (>=, not >).
        deal = calculate_rebuild_deal(
            purchase_price=200_000,
            property_sqft=2000,
            cost_per_sqft=150,
            profit_margin=0.20,
            market_value_estimate=600_000,  # == required_sale_price
        )
        assert deal.required_sale_price == 600_000
        assert deal.is_worth_it is True

    def test_boundary_one_dollar_below_required_sale_price(self):
        deal = calculate_rebuild_deal(
            purchase_price=200_000,
            property_sqft=2000,
            cost_per_sqft=150,
            profit_margin=0.20,
            market_value_estimate=599_999,
        )
        assert deal.is_worth_it is False
