"""
Rebuild deal math — the plan's Core Calculation, implemented as a single
pure function. No I/O, no DB, no external calls: five inputs in, five
outputs out. This is also the source of truth ported to client-side JS in
Phase 4 for the live cost/sqft + margin sliders, so keep it simple enough
to translate directly.

    build_cost          = cost_per_sqft * property_sqft
    total_cost           = purchase_price + build_cost
    required_sale_price  = total_cost * (1 + profit_margin)
    achievable_margin    = (market_value_estimate - total_cost) / total_cost
    is_worth_it          = market_value_estimate >= required_sale_price
"""

from dataclasses import dataclass


@dataclass
class RebuildDeal:
    build_cost: float
    total_cost: float
    required_sale_price: float
    achievable_margin: float
    is_worth_it: bool


def calculate_rebuild_deal(purchase_price, property_sqft, cost_per_sqft, profit_margin, market_value_estimate):
    """
    profit_margin is a fraction (0.20 for a 20% target margin), not a
    whole-number percentage (20).
    """
    build_cost = cost_per_sqft * property_sqft
    total_cost = purchase_price + build_cost
    required_sale_price = total_cost * (1 + profit_margin)

    # total_cost is 0 only in the degenerate case of a free property with
    # zero build cost — guard the division rather than let it raise.
    achievable_margin = (market_value_estimate - total_cost) / total_cost if total_cost else 0.0

    is_worth_it = market_value_estimate >= required_sale_price

    return RebuildDeal(
        build_cost=build_cost,
        total_cost=total_cost,
        required_sale_price=required_sale_price,
        achievable_margin=achievable_margin,
        is_worth_it=is_worth_it,
    )
