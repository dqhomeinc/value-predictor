from models import Analysis


def make_analysis(property_sale_history=None):
    # Only the fields sale_history_sorted actually touches matter here;
    # the rest are required by the schema but irrelevant to this test.
    return Analysis(
        user_id=1,
        address='123 Main St',
        purchase_price=200_000,
        initial_cost_per_sqft=100,
        initial_profit_margin_pct=20,
        property_sale_history=property_sale_history,
    )


class TestSaleHistorySorted:
    def test_empty_when_no_history(self):
        assert make_analysis(property_sale_history=None).sale_history_sorted == []

    def test_empty_when_history_is_empty_dict(self):
        assert make_analysis(property_sale_history={}).sale_history_sorted == []

    def test_sorts_most_recent_first(self):
        history = {
            '2017-10-19': {'event': 'Sale', 'date': '2017-10-19T00:00:00.000Z', 'price': 185000},
            '2024-11-18': {'event': 'Sale', 'date': '2024-11-18T00:00:00.000Z', 'price': 270000},
            '2020-03-02': {'event': 'Sale', 'date': '2020-03-02T00:00:00.000Z', 'price': 220000},
        }
        sorted_history = make_analysis(property_sale_history=history).sale_history_sorted

        assert [entry['price'] for entry in sorted_history] == [270000, 220000, 185000]
        assert sorted_history[0]['date'] == '2024-11-18T00:00:00.000Z'
