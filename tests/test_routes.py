import os
import re

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

    DATABASE_URL/SECRET_KEY have to be set in the environment *before*
    create_app() runs, not as flask_app.config[...] overrides afterward —
    create_app() reads them and calls db.init_app() internally, which
    binds the engine immediately. Setting config after the fact is too
    late to change that binding. (Found the hard way: without this, every
    test in this file was silently running against the real local
    instance/value_predictor.db instead of an isolated in-memory one.)
    """
    old_database_url = os.environ.get('DATABASE_URL')
    old_secret_key = os.environ.get('SECRET_KEY')
    os.environ['DATABASE_URL'] = 'sqlite:///:memory:'
    os.environ['SECRET_KEY'] = 'test-secret'
    try:
        flask_app = create_app()
    finally:
        for key, old_value in (('DATABASE_URL', old_database_url), ('SECRET_KEY', old_secret_key)):
            if old_value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = old_value

    flask_app.config['TESTING'] = True
    flask_app.config['WTF_CSRF_ENABLED'] = False
    with flask_app.app_context():
        yield flask_app
        # Dispose the engine when this module's tests are done, so the
        # next test file's Flask app (a different instance, same shared
        # `db` object) starts from a clean connection pool rather than
        # whatever this one leaves behind.
        db.engine.dispose()


@pytest.fixture
def client(flask_app):
    with flask_app.app_context():
        db.create_all()
        yield flask_app.test_client()
        db.session.remove()
        db.drop_all()
        db.engine.dispose()


def make_logged_in_analysis(
    client,
    property_sale_history=None,
    property_latitude=None,
    property_longitude=None,
    market_value_comps_snapshot=None,
    zoning_detail=None,
    market_value_method='rentcast_avm',
    property_lot_size=None,
):
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
        market_value_method=market_value_method,
        market_value_confidence='high',
        market_value_comps_count=3,
        market_value_comps_snapshot=market_value_comps_snapshot,
        zoning_detail=zoning_detail,
        property_lot_size=property_lot_size,
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


SAMPLE_COMPS = [
    {
        'formattedAddress': '456 Oak Ave, Austin, TX',
        'price': 480_000,
        'bedrooms': 3,
        'bathrooms': 2,
        'squareFootage': 1900,
        'distance': 2.3,
    },
    {
        'formattedAddress': '789 Pine Ln, Austin, TX',
        'price': 510_000,
        'bedrooms': 4,
        'bathrooms': 2.5,
        'squareFootage': 2200,
        'distance': 8.1,
    },
    {
        'formattedAddress': '12 Cedar Ct, Austin, TX',
        'price': 460_000,
        'bedrooms': 3,
        'bathrooms': 2,
        'squareFootage': 1850,
        'distance': 14.9,
    },
]


class TestAnalysisResultsNearbySales:
    def test_renders_radius_selector_and_comps_data_when_present(self, client):
        analysis = make_logged_in_analysis(client, market_value_comps_snapshot=SAMPLE_COMPS)

        response = client.get(f'/analyses/{analysis.id}')
        html = response.data.decode()

        assert response.status_code == 200
        assert 'class="radius-btn" data-radius="5"' in html
        assert 'class="radius-btn" data-radius="10"' in html
        assert 'class="radius-btn" data-radius="15"' in html
        assert 'id="nearby-sales-data"' in html
        assert 'nearby-sales.js' in html
        assert '456 Oak Ave, Austin, TX' in html

    def test_renders_fallback_when_no_comps(self, client):
        analysis = make_logged_in_analysis(client, market_value_comps_snapshot=None)

        response = client.get(f'/analyses/{analysis.id}')
        html = response.data.decode()

        assert response.status_code == 200
        assert 'No comparable sales available' in html
        assert 'radius-btn' not in html
        assert 'nearby-sales.js' not in html

    def test_renders_fallback_when_comps_is_empty_list(self, client):
        analysis = make_logged_in_analysis(client, market_value_comps_snapshot=[])

        response = client.get(f'/analyses/{analysis.id}')
        html = response.data.decode()

        assert response.status_code == 200
        assert 'No comparable sales available' in html


class TestAnalysisResultsCompCachedBanner:
    def test_shows_limited_data_banner_and_refresh_form_for_comp_cached(self, client):
        analysis = make_logged_in_analysis(client, market_value_method='comp_cached')

        response = client.get(f'/analyses/{analysis.id}')
        html = response.data.decode()

        assert response.status_code == 200
        assert 'Limited data' in html
        assert 'Refresh with full data' in html
        assert 'name="force_refresh" value="1"' in html
        # The refresh form resubmits the same inputs the analysis was
        # originally created with.
        assert f'value="{analysis.address}"' in html
        assert 'single prior comp sale/listing' in html

    def test_no_banner_for_a_normal_avm_analysis(self, client):
        analysis = make_logged_in_analysis(client, market_value_method='rentcast_avm')

        response = client.get(f'/analyses/{analysis.id}')
        html = response.data.decode()

        assert response.status_code == 200
        assert 'Limited data' not in html
        assert 'Refresh with full data' not in html


HOSTILE_ZONING_DETAIL = {
    'zoning_code': 'R-1', 'source': 'discovered', 'jurisdiction': 'X, TX',
    'in_floodplain': False, 'provenance': 'unverified',
    'zoning_description': '', 'service_title': 's', 'service_owner': 'o',
    'reference_url': 'javascript:alert(3)',
    'case_manager': {},
    'restrictions': [
        {'label': 'Local Historic Districts', 'detail': 'd',
         'severity': 'critical', 'url': 'javascript:alert(1)'},
        {'label': 'Airport Overlay', 'detail': '', 'severity': 'high',
         'url': 'https://example.gov/ok'},
    ],
    'ordinances': [
        {'number': '123', 'url': 'data:text/html,<script>alert(1)</script>'},
        {'number': '456', 'url': 'https://example.gov/ord'},
    ],
}


class TestZoningLinkSchemes:
    """Zoning detail is cached indefinitely, so a row stored before the
    ingestion filter existed can still hold a hostile URL. These cover the
    render-time guard on already-stored data."""

    def test_dangerous_schemes_are_never_rendered_as_links(self, client):
        analysis = make_logged_in_analysis(client, zoning_detail=HOSTILE_ZONING_DETAIL)

        html = client.get(f'/analyses/{analysis.id}').data.decode()

        assert 'href="javascript:' not in html.lower()
        assert 'href="data:' not in html.lower()

    def test_legitimate_links_and_all_text_still_render(self, client):
        analysis = make_logged_in_analysis(client, zoning_detail=HOSTILE_ZONING_DETAIL)

        html = client.get(f'/analyses/{analysis.id}').data.decode()

        # Dropping the scheme must not drop the information itself.
        assert 'https://example.gov/ok' in html
        assert 'https://example.gov/ord' in html
        assert 'Local Historic Districts' in html
        assert '123' in html


def _build_restrictions_text(html):
    start = html.find('<h3>Build restrictions</h3>')
    section = html[start:html.find('</section>', start)]
    text = re.sub(r'<[^>]+>', ' ', section)
    text = text.replace('&ldquo;', '"').replace('&rdquo;', '"').replace('&#39;', "'")
    return ' '.join(text.split())


# The Lexington, MA result that reached production: a code, no description,
# from a layer published by a personal account.
LEXINGTON_DISCOVERED = {
    'zoning_code': 'RS', 'source': 'discovered', 'jurisdiction': 'Lexington, MA',
    'in_floodplain': False, 'ordinances': [], 'case_manager': {}, 'zoning_description': '',
    'provenance': 'unverified', 'service_title': 'TownOwnedParcels', 'service_owner': 'jBaldasaro',
    'reference_url': '', 'layer_name': 'ZONING', 'data_updated': '2022-05-11',
    'flood_zone': {'zone': 'X', 'subtype': 'AREA OF MINIMAL FLOOD HAZARD', 'in_sfha': False, 'source': 'FEMA NFHL'},
    'detail_version': 2, 'restrictions': [],
}


class TestDiscoveredZoningDisplay:
    def _render(self, client, detail):
        analysis = make_logged_in_analysis(client, zoning_detail=detail)
        response = client.get(f'/analyses/{analysis.id}')
        assert response.status_code == 200
        return _build_restrictions_text(response.data.decode())

    def test_shows_the_district_even_without_a_description(self, client):
        # The section used to show only caveats when a layer carried a code
        # but no plain-English description, so it read as "found something,
        # won't say what".
        text = self._render(client, LEXINGTON_DISCOVERED)

        assert 'Zoning district: RS' in text

    def test_reports_what_was_verified_rather_than_an_officialness_claim(self, client):
        # Account names can't establish who's official: `lexingtonGIS`
        # published Minnesota data. What's knowable is which layer matched
        # the parcel and when its data was last updated.
        text = self._render(client, LEXINGTON_DISCOVERED)

        assert "couldn't automatically confirm" not in text
        assert 'official' not in text.lower()
        assert '"ZONING" layer of "TownOwnedParcels"' in text
        assert 'published on ArcGIS by jBaldasaro' in text
        assert 'last updated 2022-05-11' in text

    def test_shows_the_fema_flood_zone(self, client):
        text = self._render(client, LEXINGTON_DISCOVERED)

        assert 'FEMA flood zone: X — area of minimal flood hazard' in text
        assert "FEMA's national flood maps" in text

    def test_special_flood_hazard_area_leads_with_the_warning(self, client):
        detail = {
            **LEXINGTON_DISCOVERED,
            'in_floodplain': True,
            'flood_zone': {'zone': 'AE', 'subtype': '', 'in_sfha': True, 'source': 'FEMA NFHL'},
            'restrictions': [{'label': 'FEMA Special Flood Hazard Area', 'detail': 'Zone AE',
                              'severity': 'critical', 'url': ''}],
        }

        text = self._render(client, detail)

        assert 'can block a teardown or restrict the rebuild' in text
        assert 'FEMA Special Flood Hazard Area' in text
        assert 'FEMA flood zone: AE — inside the Special Flood Hazard Area' in text

    def test_flood_zone_without_a_district(self, client):
        detail = {**LEXINGTON_DISCOVERED, 'zoning_code': '', 'service_title': '', 'service_owner': '',
                  'layer_name': '', 'data_updated': ''}

        text = self._render(client, detail)

        assert 'No zoning district found for this address.' in text
        assert 'FEMA flood zone: X' in text

    def test_detail_saved_before_these_fields_still_renders(self, client):
        # Analyses saved earlier keep their stored detail. The page has to
        # render it without inventing facts it never recorded.
        old = {key: value for key, value in LEXINGTON_DISCOVERED.items()
               if key not in ('layer_name', 'data_updated', 'flood_zone', 'detail_version')}

        text = self._render(client, old)

        assert 'Zoning district: RS' in text
        assert "doesn't say when" not in text


class TestWhatYouCanBuild:
    def _render(self, client, detail, lot=None):
        analysis = make_logged_in_analysis(client, zoning_detail=detail, property_lot_size=lot)
        response = client.get(f'/analyses/{analysis.id}')
        assert response.status_code == 200
        return _build_restrictions_text(response.data.decode())

    def test_lexington_shows_its_bylaw_numbers(self, client):
        text = self._render(client, LEXINGTON_DISCOVERED)

        assert 'What you can build: RS (One Family Dwelling)' in text
        assert 'Front setback' in text and '30 ft' in text
        assert 'Rear setback' in text
        assert '2.5 stories / 40 ft' in text
        assert 'Lexington Zoning Bylaw, Ch. 135' in text
        assert 'amendments through the 2025 Annual Town Meeting' in text

    def test_new_home_floor_area_uses_this_lot(self, client):
        text = self._render(client, LEXINGTON_DISCOVERED, lot=14_404)

        assert '5,634 sq ft' in text
        assert 'On this 14,404 sq ft lot' in text

    def test_uncovered_town_shows_no_numbers(self, client):
        detail = {**LEXINGTON_DISCOVERED, 'jurisdiction': 'Pittsburgh, PA', 'zoning_code': 'RM-M'}

        text = self._render(client, detail)

        assert 'What you can build' not in text

    def test_atlas_numbers_are_labelled_as_a_copy(self, client):
        detail = {**LEXINGTON_DISCOVERED, 'jurisdiction': 'Concord, NH', 'zoning_code': '',
                  'atlas_standards': {
                      'state': 'NH', 'jurisdiction': 'Concord', 'district': 'CVP',
                      'district_name': 'Civic Performance', 'single_family': 'hearing',
                      'front_ft': 15, 'side_ft': 15, 'rear_ft': 15, 'frontage_ft': 80,
                      'min_lot_acres': None, 'max_height_ft': 45, 'max_stories': None,
                      'max_lot_coverage_pct': 80, 'far': '1',
                      'source': {'citation': 'National Zoning Atlas: New Hampshire',
                                 'url': 'https://example.org/nh', 'as_of': 'data last updated 2024-03-12'}}}

        text = self._render(client, detail)

        assert 'What you can build: CVP (Civic Performance)' in text
        assert 'Needs a public hearing' in text
        assert 'National Zoning Atlas: New Hampshire' in text
        assert 'digitized copy' in text

    def test_atlas_district_that_disagrees_with_the_town_map_is_flagged(self, client):
        detail = {**LEXINGTON_DISCOVERED, 'jurisdiction': 'Concord, NH', 'zoning_code': 'RN',
                  'atlas_standards': {'jurisdiction': 'Concord', 'district': 'CVP', 'front_ft': 15,
                                      'source': {'citation': 'Atlas', 'url': '', 'as_of': ''}}}

        text = self._render(client, detail)

        assert "town's map shows RN" in text

    def test_hostile_source_url_is_not_linked(self, client):
        detail = {**LEXINGTON_DISCOVERED, 'jurisdiction': 'Concord, NH', 'zoning_code': '',
                  'atlas_standards': {'jurisdiction': 'Concord', 'front_ft': 15,
                                      'source': {'citation': 'Atlas', 'url': 'javascript:alert(1)', 'as_of': ''}}}
        analysis = make_logged_in_analysis(client, zoning_detail=detail)

        html = client.get(f'/analyses/{analysis.id}').data.decode()

        assert 'href="javascript:' not in html
