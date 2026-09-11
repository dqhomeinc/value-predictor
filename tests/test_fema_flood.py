import pytest
import requests

from integrations.fema_flood import FloodZone, lookup_flood_zone


class FakeResponse:
    def __init__(self, payload, status_code=200, bad_json=False):
        self.status_code = status_code
        self._payload = payload
        self._bad_json = bad_json

    def raise_for_status(self):
        if self.status_code >= 400:
            raise requests.HTTPError(str(self.status_code))

    def json(self):
        if self._bad_json:
            raise ValueError('not json')
        return self._payload


class FakeSession:
    def __init__(self, response=None, exc=None):
        self.response = response
        self.exc = exc
        self.calls = []

    def get(self, url, params=None, timeout=None, headers=None):
        self.calls.append((url, params))
        if self.exc:
            raise self.exc
        return self.response


def features(*rows):
    return {'features': [{'attributes': row} for row in rows]}


class TestLookupFloodZone:
    def test_inside_the_special_flood_hazard_area(self):
        # Miami Beach, verified against the live service.
        session = FakeSession(FakeResponse(features({'FLD_ZONE': 'AE', 'ZONE_SUBTY': None, 'SFHA_TF': 'T'})))

        result = lookup_flood_zone(-80.13, 25.79, session=session)

        assert result == FloodZone(zone='AE', subtype='', in_sfha=True)

    def test_minimal_hazard_zone_x(self):
        # Lexington MA, verified against the live service.
        session = FakeSession(FakeResponse(features(
            {'FLD_ZONE': 'X', 'ZONE_SUBTY': 'AREA OF MINIMAL FLOOD HAZARD', 'SFHA_TF': 'F'})))

        result = lookup_flood_zone(-71.19, 42.43, session=session)

        assert result.zone == 'X'
        assert result.in_sfha is False
        assert result.subtype == 'AREA OF MINIMAL FLOOD HAZARD'

    def test_the_more_hazardous_polygon_wins_on_a_boundary(self):
        session = FakeSession(FakeResponse(features(
            {'FLD_ZONE': 'X', 'ZONE_SUBTY': 'AREA OF MINIMAL FLOOD HAZARD', 'SFHA_TF': 'F'},
            {'FLD_ZONE': 'AE', 'ZONE_SUBTY': None, 'SFHA_TF': 'T'},
        )))

        assert lookup_flood_zone(0, 0, session=session).zone == 'AE'

    def test_queries_the_point_in_wgs84(self):
        session = FakeSession(FakeResponse(features({'FLD_ZONE': 'X', 'SFHA_TF': 'F'})))

        lookup_flood_zone(-71.19, 42.43, session=session)

        url, params = session.calls[0]
        assert 'NFHL/MapServer/28/query' in url
        assert params['geometry'] == '-71.19,42.43'
        assert params['inSR'] == 4326

    @pytest.mark.parametrize('response', [
        FakeResponse({'features': []}),                       # unmapped area
        FakeResponse({'error': {'code': 500}}),               # service-level error
        FakeResponse(None, status_code=503),                  # FEMA down
        FakeResponse(None, bad_json=True),                    # not JSON
        FakeResponse(['not', 'a', 'dict']),                   # unexpected shape
        FakeResponse({'features': [{'attributes': {}}]}),     # no zone on the feature
        FakeResponse({'features': ['junk']}),
    ])
    def test_every_failure_is_none_never_an_exception(self, response):
        # Informational only — a FEMA hiccup must never fail an analysis.
        assert lookup_flood_zone(0, 0, session=FakeSession(response)) is None

    def test_network_failure_is_none(self):
        session = FakeSession(exc=requests.ConnectionError('down'))

        assert lookup_flood_zone(0, 0, session=session) is None

    def test_as_dict_is_json_safe(self):
        zone = FloodZone(zone='AE', subtype='', in_sfha=True)

        assert zone.as_dict() == {'zone': 'AE', 'subtype': '', 'in_sfha': True, 'source': 'FEMA NFHL'}
