import pytest

from app import create_app
from models import Analysis, User, db


@pytest.fixture(scope='module')
def flask_app():
    """The real create_app() factory, not conftest's minimal `app` fixture
    — needed here because the bug this file guards against (unguarded
    formatting in analysis_results.html) only manifests during actual
    template rendering, not at the model layer.

    Module-scoped deliberately: create_app() re-initializes module-level
    singletons (login_manager, migrate, csrf) against whichever Flask app
    instance it's given, and calling it fresh per test caused state
    leakage between tests within the same process. Call it once per
    module; reset only the database per test via the `client` fixture.
    """
    flask_app = create_app()
    flask_app.config['TESTING'] = True
    flask_app.config['WTF_CSRF_ENABLED'] = False
    flask_app.config['SQLALCHEMY_DATABASE_URI'] = 'sqlite:///:memory:'
    flask_app.config['SECRET_KEY'] = 'test-secret'
    with flask_app.app_context():
        yield flask_app


@pytest.fixture
def client(flask_app):
    with flask_app.app_context():
        db.create_all()
        yield flask_app.test_client()
        db.session.remove()
        db.drop_all()


def make_logged_in_analysis(client, property_sale_history=None, property_latitude=None, property_longitude=None):
    client.post('/register', data={
        'username': 'flipper', 'email': 'f@example.com', 'password': 'correcthorsebatterystaple',
    })
    user = User.query.filter_by(email='f@example.com').first()

    analysis = Analysis(
        user_id=user.id,
        address='123 Main St, Austin, TX',
        purchase_price=200_000,
        initial_cost_per_sqft=100,
        initial_profit_margin_pct=20,
        property_sqft=2000,
        market_value_estimate=500_000,
        market_value_method='rentcast_avm',
        market_value_confidence='high',
        market_value_comps_count=3,
        build_cost_estimate=200_000,
        total_cost_estimate=400_000,
        required_sale_price=480_000,
        achievable_margin_pct=25.0,
        is_worth_it=True,
        property_sale_history=property_sale_history,
        property_latitude=property_latitude,
        property_longitude=property_longitude,
    )
    db.session.add(analysis)
    db.session.commit()
    return analysis


class TestAnalysisResultsTransactionHistory:
    def test_renders_entry_with_missing_price_without_500(self, client):
        # A non-disclosure-state sale, or a non-"Sale" event type — RentCast
        # doesn't guarantee every history entry has a price.
        analysis = make_logged_in_analysis(client, property_sale_history={
            '2024-11-18': {'event': 'Sale', 'date': '2024-11-18T00:00:00.000Z', 'price': None},
        })

        response = client.get(f'/analyses/{analysis.id}')

        assert response.status_code == 200
        assert b'price undisclosed' in response.data

    def test_renders_entry_with_missing_date_without_500(self, client):
        analysis = make_logged_in_analysis(client, property_sale_history={
            '2024-11-18': {'event': 'Sale', 'date': None, 'price': 270000},
        })

        response = client.get(f'/analyses/{analysis.id}')

        assert response.status_code == 200
        assert b'Unknown date' in response.data

    def test_renders_normal_entry_correctly(self, client):
        analysis = make_logged_in_analysis(client, property_sale_history={
            '2024-11-18': {'event': 'Sale', 'date': '2024-11-18T00:00:00.000Z', 'price': 270000},
        })

        response = client.get(f'/analyses/{analysis.id}')

        assert response.status_code == 200
        assert b'2024-11-18' in response.data
        assert b'270,000' in response.data

    def test_renders_fallback_when_no_history(self, client):
        analysis = make_logged_in_analysis(client, property_sale_history=None)

        response = client.get(f'/analyses/{analysis.id}')

        assert response.status_code == 200
        assert b'No sale history available' in response.data


class TestAnalysisResultsMap:
    def test_renders_map_when_coordinates_present(self, client):
        analysis = make_logged_in_analysis(client, property_latitude=30.2849, property_longitude=-97.7341)

        response = client.get(f'/analyses/{analysis.id}')
        html = response.data.decode()

        assert response.status_code == 200
        assert 'id="property-map"' in html
        assert 'leaflet.js' in html
        assert 'leaflet.css' in html
        assert '30.2849' in html
        assert '-97.7341' in html

    def test_renders_fallback_when_coordinates_missing(self, client):
        analysis = make_logged_in_analysis(client, property_latitude=None, property_longitude=None)

        response = client.get(f'/analyses/{analysis.id}')
        html = response.data.decode()

        assert response.status_code == 200
        assert 'No location data available' in html
        # Leaflet shouldn't even load if there's nothing to show.
        assert 'leaflet.js' not in html
        assert 'id="property-map"' not in html

    def test_renders_fallback_when_only_one_coordinate_present(self, client):
        # Defensive: a partial/malformed response shouldn't half-render a map.
        analysis = make_logged_in_analysis(client, property_latitude=30.2849, property_longitude=None)

        response = client.get(f'/analyses/{analysis.id}')
        html = response.data.decode()

        assert response.status_code == 200
        assert 'No location data available' in html
