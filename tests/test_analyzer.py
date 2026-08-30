import pytest

from integrations.rentcast import RentCastClient, RentCastNotFoundError
from models import Analysis, User, db
from services.analyzer import AnalysisError, run_analysis
from services.market_value import MarketValueUnavailableError

VALUE_ESTIMATE_RESPONSE = {
    'price': 500_000,
    'priceRangeLow': 480_000,
    'priceRangeHigh': 520_000,
    'subjectProperty': {
        'squareFootage': 2000,
        'lotSize': 6000,
        'bedrooms': 3,
        'bathrooms': 2,
        'yearBuilt': 1965,
        'latitude': 30.2849,
        'longitude': -97.7341,
    },
    'comparables': [{'price': 490_000}, {'price': 500_000}, {'price': 510_000}],
}

PROPERTY_RECORD_RESPONSE = [
    {
        'zoning': 'R-1',
        'subdivision': 'Grand Lake Estates',
        'history': {
            '2017-10-19': {'event': 'Sale', 'date': '2017-10-19T00:00:00.000Z', 'price': 185000},
            '2024-11-18': {'event': 'Sale', 'date': '2024-11-18T00:00:00.000Z', 'price': 270000},
        },
    }
]


class FakeResponse:
    def __init__(self, status_code, payload=None):
        self.status_code = status_code
        self.ok = 200 <= status_code < 300
        self._payload = payload
        self.text = str(payload)

    def json(self):
        return self._payload


class FakeSession:
    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = []

    def get(self, url, params=None, headers=None, timeout=None):
        self.calls.append(url)
        return self.responses.pop(0)


def make_client(responses):
    return RentCastClient(api_key='test-key', session=FakeSession(responses))


@pytest.fixture
def user(app):
    u = User(username='flipper', email='flipper@example.com')
    u.set_password('correct horse battery staple')
    db.session.add(u)
    db.session.commit()
    return u


class TestRunAnalysis:
    def test_success_persists_analysis_with_correct_math(self, app, user):
        client = make_client([
            FakeResponse(200, VALUE_ESTIMATE_RESPONSE),
            FakeResponse(200, PROPERTY_RECORD_RESPONSE),
        ])

        analysis = run_analysis(
            user=user,
            address='123 Main St, Austin, TX',
            purchase_price=200_000,
            cost_per_sqft=100,
            profit_margin_pct=20,
            rentcast_client=client,
        )

        # Persisted and retrievable.
        assert analysis.id is not None
        assert Analysis.query.count() == 1

        # Property characteristics from the AVM's subjectProperty.
        assert analysis.property_sqft == 2000
        assert analysis.property_lot_size == 6000
        assert analysis.property_bedrooms == 3
        assert analysis.property_bathrooms == 2
        assert analysis.property_year_built == 1965

        # Zoning/subdivision from Property Records.
        assert analysis.property_zoning == 'R-1'
        assert analysis.property_subdivision == 'Grand Lake Estates'

        # Sale history from Property Records, stored raw; sorted view is
        # most-recent-first (covered directly in tests/test_models.py).
        assert analysis.property_sale_history == PROPERTY_RECORD_RESPONSE[0]['history']
        assert analysis.sale_history_sorted[0]['price'] == 270000

        # Coordinates for the results-page map, from the AVM's subjectProperty.
        assert analysis.property_latitude == 30.2849
        assert analysis.property_longitude == -97.7341

        # Market value: AVM price directly, comps count == 3, high
        # confidence (>=3 comps, tight range: 40k/500k = 8%).
        assert analysis.market_value_estimate == 500_000
        assert analysis.market_value_method == 'rentcast_avm'
        assert analysis.market_value_confidence == 'high'
        assert analysis.market_value_comps_count == 3

        # Deal math: build_cost = 100 * 2000 = 200_000; total_cost =
        # 200_000 + 200_000 = 400_000; required_sale = 400_000 * 1.20 =
        # 480_000; market value 500_000 >= 480_000 -> worth it.
        assert analysis.build_cost_estimate == 200_000
        assert analysis.total_cost_estimate == 400_000
        assert analysis.required_sale_price == 480_000
        assert analysis.achievable_margin_pct == pytest.approx(25.0)  # (500k-400k)/400k
        assert analysis.is_worth_it is True

        # Inputs stored as submitted, for the results page's initial slider values.
        assert analysis.initial_cost_per_sqft == 100
        assert analysis.initial_profit_margin_pct == 20

    def test_rentcast_error_propagates_and_persists_nothing(self, app, user):
        client = make_client([FakeResponse(404, None)])

        with pytest.raises(RentCastNotFoundError):
            run_analysis(
                user=user,
                address='1 Nowhere Rd',
                purchase_price=200_000,
                cost_per_sqft=100,
                profit_margin_pct=20,
                rentcast_client=client,
            )

        assert Analysis.query.count() == 0

    def test_market_value_unavailable_propagates_and_persists_nothing(self, app, user):
        avm_json = {
            'price': None,
            'subjectProperty': {'squareFootage': 2000},
            'comparables': [],  # nothing to fall back on
        }
        client = make_client([
            FakeResponse(200, avm_json),
            FakeResponse(200, PROPERTY_RECORD_RESPONSE),
        ])

        with pytest.raises(MarketValueUnavailableError):
            run_analysis(
                user=user,
                address='123 Main St, Austin, TX',
                purchase_price=200_000,
                cost_per_sqft=100,
                profit_margin_pct=20,
                rentcast_client=client,
            )

        assert Analysis.query.count() == 0

    def test_missing_square_footage_raises_analysis_error(self, app, user):
        avm_json = {**VALUE_ESTIMATE_RESPONSE, 'subjectProperty': {**VALUE_ESTIMATE_RESPONSE['subjectProperty'], 'squareFootage': None}}
        client = make_client([
            FakeResponse(200, avm_json),
            FakeResponse(200, PROPERTY_RECORD_RESPONSE),
        ])

        with pytest.raises(AnalysisError):
            run_analysis(
                user=user,
                address='123 Main St, Austin, TX',
                purchase_price=200_000,
                cost_per_sqft=100,
                profit_margin_pct=20,
                rentcast_client=client,
            )

        assert Analysis.query.count() == 0

    def test_second_analysis_of_same_address_costs_zero_rentcast_calls(self, app, user):
        client = make_client([
            FakeResponse(200, VALUE_ESTIMATE_RESPONSE),
            FakeResponse(200, PROPERTY_RECORD_RESPONSE),
        ])

        run_analysis(
            user=user,
            address='123 Main St, Austin, TX',
            purchase_price=200_000,
            cost_per_sqft=100,
            profit_margin_pct=20,
            rentcast_client=client,
        )
        # Different inputs, same address -> should reuse the cached
        # property data rather than hitting RentCast again.
        run_analysis(
            user=user,
            address='123 Main St, Austin, TX',
            purchase_price=250_000,
            cost_per_sqft=120,
            profit_margin_pct=15,
            rentcast_client=client,
        )

        assert len(client.session.calls) == 2  # not 4
        assert Analysis.query.count() == 2
