import pytest
import requests

from integrations.municipal_zoning import (
    MunicipalZoningUnavailableError,
    lookup_municipal_zoning,
)

GEOCODE_SUCCESS = {
    'candidates': [
        {'address': '4100 AVENUE G, AUSTIN, TX, 78751', 'score': 100, 'location': {'x': -97.7295, 'y': 30.3035}},
    ],
}


def identify(*results):
    return {'results': list(results)}


def match(layer, **attrs):
    return {'layerName': layer, 'attributes': attrs}


# Mirrors a real response set for a Hyde Park parcel — verified against
# the live services while building this adapter.
ZONING_1 = identify(
    match('Zoning', Zoning='SF-3-HD-NCCD-NP', ZONING_BASE='SF'),
    match('Zoning Ordinance', **{'Ordinance Number': '20101216-093',
                                 'Ordinance hyperlink': 'http://example.gov/ord/146912'}),
    match('Zoning Case Managers', **{'Zoning Case Manager': 'Cynthia Hadri', 'Phone Number': '(512)974-7620'}),
)
ZONING_2 = identify(
    match('Neighborhood Conservation Combining District', **{'Sub Name': 'HYDE PARK'}),
    match('Residential Design Standards', **{'Source Document': 'LDC/25-2-Subchapter F'}),
)
ZONING_3 = identify(match('Local Historic Districts', **{'Ordinance Number': '20101216-093'}))
FLOODPLAIN_EMPTY = identify()
PROPERTY = identify(match('Jurisdictions (No Fill)', Jurisdiction='AUSTIN FULL PURPOSE'))


class FakeResponse:
    def __init__(self, status_code, payload):
        self.status_code = status_code
        self._payload = payload

    def raise_for_status(self):
        if self.status_code >= 400:
            raise requests.HTTPError(f'{self.status_code} error')

    def json(self):
        return self._payload


class FakeSession:
    """Dispatches on URL rather than a strict call queue — the adapter
    fans out across several MapServers, and order shouldn't be baked into
    every test. `routes` maps a URL substring to a payload or an exception
    to raise."""

    def __init__(self, routes):
        self.routes = routes
        self.calls = []

    def get(self, url, params=None, timeout=None, headers=None):
        self.calls.append(url)
        for fragment, payload in self.routes.items():
            if fragment in url:
                if isinstance(payload, Exception):
                    raise payload
                if isinstance(payload, FakeResponse):
                    return payload
                return FakeResponse(200, payload)
        return FakeResponse(200, identify())


def austin_session(**overrides):
    routes = {
        'GeocodeServer': GEOCODE_SUCCESS,
        'Zoning_1': ZONING_1,
        'Zoning_2': ZONING_2,
        'Zoning_3': ZONING_3,
        'Floodplain': FLOODPLAIN_EMPTY,
        'Property': PROPERTY,
    }
    routes.update(overrides)
    return FakeSession(routes)


class TestJurisdictionDetection:
    @pytest.mark.parametrize('address', [
        '4100 Avenue G, Austin, TX',
        '4100 avenue g, austin, tx 78751',
        '123 Main St, AUSTIN, TX',
    ])
    def test_recognizes_austin_addresses(self, address):
        result = lookup_municipal_zoning(address, session=austin_session())

        assert result.zoning_code == 'SF-3-HD-NCCD-NP'
        assert result.source == 'austin_gis'

    @pytest.mark.parametrize('address', [
        '4100 Avenue G Austin TX',           # no commas — people type these
        '4100 Avenue G Austin TX 78751',
        '4100 Avenue G, Austin, Texas',      # state spelled out
        '4100 Avenue G, Austin, TX 78751, USA',
    ])
    def test_recognizes_austin_without_requiring_commas(self, address):
        result = lookup_municipal_zoning(address, session=austin_session())

        assert result.zoning_code == 'SF-3-HD-NCCD-NP'

    @pytest.mark.parametrize('address', [
        '123 Austin Ave, Georgetown, TX',
        '500 W Austin St, Marble Falls, TX',
        '9 Austin Hwy, San Antonio, TX',
        '1 Austin Business Park, Round Rock, TX',
    ])
    def test_austin_as_a_street_name_is_not_claimed_by_the_austin_adapter(self, address):
        # These are elsewhere in Texas. Handing them to Austin's geocoder
        # would fuzzy-match a similarly named Austin street and return
        # another parcel's rules as if they were this one's. They fall
        # through to discovery instead, which resolves the real
        # jurisdiction from the address rather than assuming it.
        session = austin_session()

        with pytest.raises(MunicipalZoningUnavailableError):
            lookup_municipal_zoning(address, session=session)

        assert not any('austintexas.gov' in url for url in session.calls), \
            'a non-Austin address must never reach the Austin adapter'

    def test_address_without_a_curated_adapter_falls_through_to_discovery(self):
        # No curated adapter for Springfield, so the nationwide discovery
        # path takes over (integrations/zoning_discovery.py). This session
        # routes nothing for the Census geocoder, so discovery finds
        # nothing and the caller still sees the same "no answer" exception.
        session = austin_session()

        with pytest.raises(MunicipalZoningUnavailableError):
            lookup_municipal_zoning('123 Main St, Springfield, IL', session=session)

        assert any('geocoding.geo.census.gov' in url for url in session.calls), \
            'expected discovery to attempt a Census geocode'


class TestAustinRestrictions:
    def test_collects_restrictions_with_teardown_relevant_severities(self):
        result = lookup_municipal_zoning('4100 Avenue G, Austin, TX', session=austin_session())

        by_label = {r.label: r for r in result.restrictions}
        # A historic district can block demolition outright — the whole
        # reason this feature exists, so it must rank critical.
        assert by_label['Local Historic Districts'].severity == 'critical'
        assert by_label['Neighborhood Conservation Combining District'].severity == 'high'
        assert by_label['Neighborhood Conservation Combining District'].detail == 'HYDE PARK'
        assert by_label['Residential Design Standards'].severity == 'high'

    def test_critical_restrictions_sort_first(self):
        result = lookup_municipal_zoning('4100 Avenue G, Austin, TX', session=austin_session())

        severities = [r.severity for r in result.restrictions]
        assert severities == sorted(severities, key=lambda s: {'critical': 0, 'high': 1, 'info': 2}[s])

    def test_captures_jurisdiction_ordinances_and_case_manager(self):
        result = lookup_municipal_zoning('4100 Avenue G, Austin, TX', session=austin_session())

        assert result.jurisdiction == 'AUSTIN FULL PURPOSE'
        assert result.ordinances == [{'number': '20101216-093', 'url': 'http://example.gov/ord/146912'}]
        assert result.case_manager == {'name': 'Cynthia Hadri', 'phone': '(512)974-7620'}

    def test_floodplain_hit_becomes_a_critical_restriction(self):
        session = austin_session(Floodplain=identify(match('Floodplain', Name='100-year')))

        result = lookup_municipal_zoning('4100 Avenue G, Austin, TX', session=session)

        assert result.in_floodplain is True
        assert result.restrictions[0].severity == 'critical'
        assert 'floodplain' in result.restrictions[0].label.lower()

    def test_no_overlays_still_succeeds_with_just_a_zoning_code(self):
        session = austin_session(Zoning_2=identify(), Zoning_3=identify())

        result = lookup_municipal_zoning('4100 Avenue G, Austin, TX', session=session)

        assert result.zoning_code == 'SF-3-HD-NCCD-NP'
        assert result.restrictions == []
        assert result.in_floodplain is False

    def test_noise_layers_are_not_reported_as_restrictions(self):
        session = austin_session(Property=identify(
            match('Jurisdictions (No Fill)', Jurisdiction='AUSTIN FULL PURPOSE'),
            match('Streets', Name='AVENUE G'),
            match('Counties', **{'County Name': 'TRAVIS'}),
        ))

        result = lookup_municipal_zoning('4100 Avenue G, Austin, TX', session=session)

        assert 'Streets' not in {r.label for r in result.restrictions}
        assert 'Counties' not in {r.label for r in result.restrictions}

    def test_as_dict_is_json_safe_and_complete(self):
        result = lookup_municipal_zoning('4100 Avenue G, Austin, TX', session=austin_session()).as_dict()

        assert result['zoning_code'] == 'SF-3-HD-NCCD-NP'
        assert result['jurisdiction'] == 'AUSTIN FULL PURPOSE'
        assert result['in_floodplain'] is False
        assert all(set(r) == {'label', 'detail', 'severity', 'url'} for r in result['restrictions'])


class TestAustinFailureModes:
    def test_no_geocode_candidates_raises(self):
        session = austin_session(GeocodeServer={'candidates': []})

        with pytest.raises(MunicipalZoningUnavailableError):
            lookup_municipal_zoning('9999 Nowhere Rd, Austin, TX', session=session)

    def test_low_confidence_geocode_match_raises(self):
        session = austin_session(GeocodeServer={
            'candidates': [{'address': 'x', 'score': 42, 'location': {'x': 1, 'y': 2}}],
        })

        with pytest.raises(MunicipalZoningUnavailableError):
            lookup_municipal_zoning('some vague address, Austin, TX', session=session)

    def test_no_zoning_polygon_raises(self):
        # Geocodes fine but falls outside city zoning (e.g. the ETJ).
        session = austin_session(Zoning_1=identify())

        with pytest.raises(MunicipalZoningUnavailableError):
            lookup_municipal_zoning('4100 Avenue G, Austin, TX', session=session)

    def test_geocoder_network_failure_raises(self):
        session = austin_session(GeocodeServer=requests.ConnectionError('boom'))

        with pytest.raises(MunicipalZoningUnavailableError):
            lookup_municipal_zoning('4100 Avenue G, Austin, TX', session=session)

    def test_geocoder_http_error_raises(self):
        session = austin_session(GeocodeServer=FakeResponse(500, {}))

        with pytest.raises(MunicipalZoningUnavailableError):
            lookup_municipal_zoning('4100 Avenue G, Austin, TX', session=session)

    def test_one_flaky_overlay_service_does_not_lose_the_rest(self):
        # Zoning_3 (historic) times out, but zoning and the other overlays
        # still come through rather than the whole lookup failing.
        session = austin_session(Zoning_3=requests.Timeout('timed out'))

        result = lookup_municipal_zoning('4100 Avenue G, Austin, TX', session=session)

        assert result.zoning_code == 'SF-3-HD-NCCD-NP'
        assert 'Neighborhood Conservation Combining District' in {r.label for r in result.restrictions}
        assert 'Local Historic Districts' not in {r.label for r in result.restrictions}
