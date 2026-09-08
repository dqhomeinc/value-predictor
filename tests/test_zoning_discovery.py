import pytest
import requests

from integrations.zoning_discovery import (
    OFFICIAL,
    UNVERIFIED,
    Jurisdiction,
    ZoningDiscoveryError,
    _looks_like_zoning_code,
    _names_a_different_state,
    _pick_zoning_fields,
    _score_item,
    _score_layer,
    discover_zoning,
    geocode,
)

PITTSBURGH = Jurisdiction(place='Pittsburgh city', county='Allegheny County', state='PA',
                          lon=-79.9272, lat=40.4381)

CENSUS_MATCH = {
    'result': {'addressMatches': [{
        'matchedAddress': '5625 FORBES AVE, PITTSBURGH, PA, 15217',
        'coordinates': {'x': -79.9272, 'y': 40.4381},
        'geographies': {
            'Incorporated Places': [{'NAME': 'Pittsburgh city'}],
            'Counties': [{'NAME': 'Allegheny County'}],
            'States': [{'STUSAB': 'PA'}],
        },
    }]},
}


class FakeResponse:
    def __init__(self, payload, status_code=200):
        self.status_code = status_code
        self._payload = payload

    def raise_for_status(self):
        if self.status_code >= 400:
            raise requests.HTTPError(str(self.status_code))

    def json(self):
        return self._payload


class FakeSession:
    def __init__(self, routes):
        self.routes = routes
        self.calls = []
        self.headers = {}

    def get(self, url, params=None, timeout=None, headers=None):
        self.calls.append(url)
        for fragment, payload in self.routes.items():
            if fragment in url:
                if isinstance(payload, Exception):
                    raise payload
                return FakeResponse(payload)
        return FakeResponse({})


class TestZoningCodeValidation:
    @pytest.mark.parametrize('value', ['RM-M', 'SF-3-HD-NCCD-NP', 'C5-3', 'D-C', 'Village District', 'R1'])
    def test_accepts_real_zoning_codes(self, value):
        assert _looks_like_zoning_code(value)

    @pytest.mark.parametrize('value', [
        'y',                    # a Spanish-language county boundary layer's flag column
        'J',                    # a power authority's grid layer
        '19990225-070b',        # Austin's zoning *ordinance* number
        'http://example.gov/x',
        '',
        '12345678',
    ])
    def test_rejects_values_that_are_not_zoning_codes(self, value):
        # Each of these was returned by a real service during discovery
        # testing and would have been shown to a user as their zoning.
        assert not _looks_like_zoning_code(value)


class TestFieldSelection:
    def test_picks_code_and_description(self):
        code, description = _pick_zoning_fields({
            'OBJECTID': 348, 'ZON_NEW': 'RM-M',
            'Full_Zoning_Type': 'MULTI-UNIT RESIDENTIAL MODERATE DENSITY',
        })

        assert code == 'RM-M'
        assert description == 'MULTI-UNIT RESIDENTIAL MODERATE DENSITY'

    def test_ignores_ordinance_and_url_fields(self):
        # The Austin failure: field names match /zon/ but hold an ordinance
        # number and a document link, not a zoning code.
        code, _ = _pick_zoning_fields({
            'ZONING_ORDINANCE_NUMBER': '19990225-070b',
            'ZONING_ORDINANCE_PATH': 'http://www.austintexas.gov/edims/document.cfm?id=59585',
        })

        assert code == ''

    def test_returns_empty_when_nothing_zoning_shaped(self):
        assert _pick_zoning_fields({'OBJECTID': 1, 'Shape_Length': 5220.9}) == ('', '')


class TestServiceScoring:
    def test_disqualifies_a_different_state(self):
        # The Montpelier, VT -> "Middlesex County, NJ" match. Wrong-state
        # data looks entirely plausible, so it must be excluded outright
        # rather than ranked lower.
        vermont = Jurisdiction(place='Middlesex town', county='Washington County', state='VT',
                               lon=-72.6, lat=44.2)
        item = {'title': 'Base Zoning in Middlesex County, NJ', 'owner': 'someone', 'snippet': ''}

        assert _score_item(item, vermont) is None

    @pytest.mark.parametrize('snippet', [
        'Zoning districts in the City of Pittsburgh',   # 'in' -> Indiana
        'Districts or overlays for Pittsburgh',         # 'or' -> Oregon
        'Contact me for Pittsburgh zoning questions',   # 'me' -> Maine
        'Zoning ok for Pittsburgh parcels',             # 'ok' -> Oklahoma
        'Hi res zoning map of Pittsburgh',              # 'hi' -> Hawaii
    ])
    def test_ordinary_english_words_are_not_read_as_other_states(self, snippet):
        # Half the state abbreviations are common words. Treating a bare
        # 'in' or 'or' in free-text prose as a state silently discarded
        # correct services — coverage lost just as quietly as a wrong
        # answer would have been returned.
        item = {'title': 'Pittsburgh Zoning', 'owner': 'pgh.admin', 'snippet': snippet}

        assert _score_item(item, PITTSBURGH) is not None

    def test_own_state_in_the_title_is_not_self_disqualifying(self):
        item = {'title': 'Pittsburgh Zoning Districts, PA', 'owner': 'pgh.admin', 'snippet': ''}

        assert _score_item(item, PITTSBURGH) is not None

    @pytest.mark.parametrize('title,state,expected', [
        ('base zoning in middlesex county, nj', 'vt', True),   # the original failure
        ('middlesex zoning nj', 'vt', True),                   # trailing abbreviation
        ('pittsburgh zoning districts, pa', 'pa', False),      # own state
        ('zoning districts in the city of pittsburgh', 'pa', False),
        ('portland zoning, or', 'or', False),                  # Portland OR, correct
        ('portland zoning, or', 'me', True),                   # Portland ME, wrong one
    ])
    def test_state_detection_requires_a_state_shaped_position(self, title, state, expected):
        assert _names_a_different_state(title, state) is expected

    def test_disqualifies_a_service_unrelated_to_the_jurisdiction(self):
        item = {'title': 'Statewide Habitat Zones', 'owner': 'wildlife_dept', 'snippet': ''}

        assert _score_item(item, PITTSBURGH) is None

    def test_marks_a_jurisdiction_owned_service_official(self):
        item = {'title': 'Pittsburgh Zoning', 'owner': 'pittsburgh_admin', 'snippet': ''}

        score, provenance = _score_item(item, PITTSBURGH)

        assert provenance == OFFICIAL
        assert score > 0

    def test_marks_a_third_party_service_unverified(self):
        # Nashville's best hit was published by a private engineering firm.
        # Usable, but the user should be told who published it.
        item = {'title': 'Pittsburgh Zoning Districts', 'owner': 'consultant_corp', 'snippet': ''}

        _, provenance = _score_item(item, PITTSBURGH)

        assert provenance == UNVERIFIED


class TestLayerScoring:
    def test_prefers_a_zoning_layer(self):
        assert _score_layer({'name': 'Zoning Districts'}) > 0

    @pytest.mark.parametrize('name', ['Zoning Ordinance', 'Zoning Cases', 'Parcels', 'Historic Districts'])
    def test_rejects_non_base_zoning_layers(self, name):
        assert _score_layer({'name': name}) <= 0


class TestGeocode:
    def test_parses_place_county_and_state(self):
        session = FakeSession({'geocoding.geo.census.gov': CENSUS_MATCH})

        result = geocode('5625 Forbes Ave, Pittsburgh, PA', session=session)

        assert result.city == 'Pittsburgh'
        assert result.state == 'PA'
        assert result.county == 'Allegheny County'
        assert result.key == 'PITTSBURGH|ALLEGHENY COUNTY|PA'

    def test_no_match_raises(self):
        session = FakeSession({'geocoding.geo.census.gov': {'result': {'addressMatches': []}}})

        with pytest.raises(ZoningDiscoveryError):
            geocode('nowhere at all', session=session)

    def test_network_failure_raises_discovery_error(self):
        session = FakeSession({'geocoding.geo.census.gov': requests.ConnectionError('down')})

        with pytest.raises(ZoningDiscoveryError):
            geocode('5625 Forbes Ave, Pittsburgh, PA', session=session)


class TestDiscoverZoning:
    def test_end_to_end_happy_path(self):
        session = FakeSession({
            'geocoding.geo.census.gov': CENSUS_MATCH,
            'arcgis.com/sharing/rest/search': {'results': [{
                'title': 'Pittsburgh Zoning', 'owner': 'pittsburgh_admin', 'snippet': '',
                'url': 'https://services1.arcgis.com/abc/arcgis/rest/services/zoning/FeatureServer',
            }]},
            '/FeatureServer/0/query': {'features': [{'attributes': {
                'ZON_NEW': 'RM-M', 'Full_Zoning_Type': 'MULTI-UNIT RESIDENTIAL MODERATE DENSITY',
            }}]},
            '/FeatureServer': {'layers': [{'id': 0, 'name': 'Zoning', 'geometryType': 'esriGeometryPolygon'}]},
        })

        result = discover_zoning('5625 Forbes Ave, Pittsburgh, PA', session=session)

        assert result.zoning_code == 'RM-M'
        assert result.zoning_description == 'MULTI-UNIT RESIDENTIAL MODERATE DENSITY'
        assert result.provenance == OFFICIAL
        assert result.jurisdiction == 'Pittsburgh, PA'

    def test_raises_when_no_service_is_found(self):
        session = FakeSession({
            'geocoding.geo.census.gov': CENSUS_MATCH,
            'arcgis.com/sharing/rest/search': {'results': []},
        })

        with pytest.raises(ZoningDiscoveryError):
            discover_zoning('5625 Forbes Ave, Pittsburgh, PA', session=session)

    def test_raises_rather_than_returning_a_junk_code(self):
        # A matching layer whose only zoning-ish field holds junk must
        # produce no answer, not a confident wrong one.
        session = FakeSession({
            'geocoding.geo.census.gov': CENSUS_MATCH,
            'arcgis.com/sharing/rest/search': {'results': [{
                'title': 'Pittsburgh Zoning', 'owner': 'pittsburgh_admin', 'snippet': '',
                'url': 'https://services1.arcgis.com/abc/arcgis/rest/services/zoning/FeatureServer',
            }]},
            '/FeatureServer/0/query': {'features': [{'attributes': {'ZONE': 'y'}}]},
            '/FeatureServer': {'layers': [{'id': 0, 'name': 'Zoning', 'geometryType': 'esriGeometryPolygon'}]},
        })

        with pytest.raises(ZoningDiscoveryError):
            discover_zoning('5625 Forbes Ave, Pittsburgh, PA', session=session)
