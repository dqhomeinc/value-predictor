import pytest
import requests

from integrations import address_suggest
from integrations.address_suggest import normalized, suggest_addresses


def feature(**properties):
    base = {'countrycode': 'US', 'housenumber': '28', 'street': 'Lillian Road', 'city': 'Lexington',
            'state': 'Massachusetts'}
    base.update(properties)
    return {'properties': {key: value for key, value in base.items() if value is not None}}


class FakeResponse:
    def __init__(self, payload, status_code=200):
        self._payload = payload
        self.status_code = status_code

    def raise_for_status(self):
        if self.status_code >= 400:
            raise requests.HTTPError(f'{self.status_code} error')

    def json(self):
        if isinstance(self._payload, Exception):
            raise self._payload
        return self._payload


class FakeSession:
    def __init__(self, *payloads):
        self.payloads = list(payloads)
        self.calls = []

    def get(self, url, params=None, timeout=None, headers=None):
        self.calls.append(params)
        payload = self.payloads.pop(0) if len(self.payloads) > 1 else self.payloads[0]
        if isinstance(payload, Exception):
            raise payload
        return payload if isinstance(payload, FakeResponse) else FakeResponse(payload)


def photon(*features):
    return {'features': list(features)}


@pytest.fixture(autouse=True)
def clear_cache():
    address_suggest._cache.clear()
    yield
    address_suggest._cache.clear()


class TestSuggestions:
    def test_formats_a_us_street_address(self):
        session = FakeSession(photon(feature()))

        assert suggest_addresses('28 Lillian', session=session) == ['28 Lillian Road, Lexington, MA']
        assert session.calls[0]['q'] == '28 Lillian'

    def test_state_is_abbreviated_so_the_rest_of_the_app_can_parse_it(self):
        session = FakeSession(photon(feature(state='New Hampshire', city='Concord', street='Green Street')))

        assert suggest_addresses('28 Green', session=session) == ['28 Green Street, Concord, NH']

    def test_no_postcode_even_when_photon_has_one(self):
        # OSM postcodes are often a neighbouring town's, and a wrong one
        # is worse than none for matching a property later.
        session = FakeSession(photon(feature(postcode='02476')))

        assert suggest_addresses('28 Lillian', session=session) == ['28 Lillian Road, Lexington, MA']

    @pytest.mark.parametrize('properties', [
        {'countrycode': 'GB', 'city': 'Reading', 'state': 'England'},  # Photon answers worldwide
        {'housenumber': None},                                        # a street, not a property
        {'street': None},                                             # a place or POI
        {'city': None, 'county': None},
        {'state': 'Ontario'},                                         # not a US state
    ])
    def test_anything_that_is_not_a_us_street_address_is_dropped(self, properties):
        session = FakeSession(photon(feature(**properties)))

        assert suggest_addresses('28 Lillian', session=session) == []

    def test_county_stands_in_for_a_missing_city(self):
        session = FakeSession(photon(feature(city=None, county='Middlesex County')))

        assert suggest_addresses('28 Lillian', session=session) == ['28 Lillian Road, Middlesex County, MA']

    def test_duplicates_are_shown_once(self):
        # The same house twice (postcode is dropped, so they format alike)
        # plus its neighbour. No house number typed, so both houses stand.
        session = FakeSession(photon(feature(), feature(postcode='02420'), feature(housenumber='30')))

        assert suggest_addresses('Lillian Road', session=session) == [
            '28 Lillian Road, Lexington, MA', '30 Lillian Road, Lexington, MA']

    def test_limit(self):
        session = FakeSession(photon(*[feature(housenumber=str(n)) for n in range(1, 9)]))

        assert len(suggest_addresses('Lillian', session=session, limit=3)) == 3

    @pytest.mark.parametrize('query', ['', '  ', 'ab'])
    def test_short_queries_ask_nobody(self, query):
        session = FakeSession(photon(feature()))

        assert suggest_addresses(query, session=session) == []
        assert session.calls == []

    @pytest.mark.parametrize('payload', [
        requests.ConnectionError('down'),
        FakeResponse({}, status_code=500),
        FakeResponse(ValueError('not json')),
        {'features': 'nonsense'},
        {},
        [],
    ])
    def test_never_raises(self, payload):
        assert suggest_addresses('28 Lillian', session=FakeSession(payload)) == []

    def test_repeat_typing_is_served_from_the_cache(self):
        # Photon's public instance is free; the deal is not to hammer it.
        session = FakeSession(photon(feature()))

        first = suggest_addresses('28 Lillian', session=session)
        second = suggest_addresses('28  Lillian ', session=session)

        assert first == second
        assert len(session.calls) == 1


class TestNormalized:
    @pytest.mark.parametrize('one, two', [
        ('28 Lillian Rd, Lexington, MA', '28 lillian rd lexington ma'),
        ('123 Main St., Austin, TX', '123 Main St  Austin TX'),
    ])
    def test_same_address_written_differently(self, one, two):
        assert normalized(one) == normalized(two)

    def test_different_addresses(self):
        assert normalized('28 Lillian Rd, Lexington, MA') != normalized('30 Lillian Rd, Lexington, MA')


class TestRelevance:
    def test_a_different_house_number_is_not_offered(self):
        # Picking a same-street neighbour would silently analyze the
        # wrong property.
        session = FakeSession(photon(feature(housenumber='3700', street='Forbes Avenue', city='Pittsburgh',
                                             state='Pennsylvania')))

        assert suggest_addresses('5625 Forbes Ave Pitt', session=session) == []

    def test_the_typed_house_number_is_kept(self):
        session = FakeSession(photon(feature(housenumber='5625', street='Forbes Avenue', city='Pittsburgh',
                                             state='Pennsylvania')))

        assert suggest_addresses('5625 Forbes Ave Pitt', session=session) == [
            '5625 Forbes Avenue, Pittsburgh, PA']

    def test_nothing_in_common_is_not_offered(self):
        session = FakeSession(photon(feature(housenumber='999', street='North Higgins Lake Drive',
                                             city='Roscommon', state='Michigan')))

        assert suggest_addresses('zzzz nowhere 999', session=session) == []

    def test_a_house_number_and_a_first_letter_still_suggests(self):
        session = FakeSession(photon(feature()))

        assert suggest_addresses('28 Li', session=session) == ['28 Lillian Road, Lexington, MA']

    def test_a_street_typed_without_a_number(self):
        session = FakeSession(photon(feature()))

        assert suggest_addresses('Lillian Road Lex', session=session) == ['28 Lillian Road, Lexington, MA']


class TestRateLimit:
    @pytest.fixture(autouse=True)
    def clear_counters(self):
        address_suggest._lookups.clear()
        yield
        address_suggest._lookups.clear()

    def test_a_caller_cannot_keep_asking_forever(self, monkeypatch):
        monkeypatch.setattr(address_suggest, 'LOOKUPS_PER_MINUTE', 3)
        session = FakeSession(photon(feature()))

        for n in range(5):
            suggest_addresses(f'{n} Lillian Road', session=session, client=1)

        assert len(session.calls) == 3

    def test_callers_are_limited_separately(self, monkeypatch):
        monkeypatch.setattr(address_suggest, 'LOOKUPS_PER_MINUTE', 1)
        session = FakeSession(photon(feature()))

        suggest_addresses('1 Lillian Road', session=session, client=1)
        suggest_addresses('2 Lillian Road', session=session, client=1)
        suggest_addresses('3 Lillian Road', session=session, client=2)

        assert len(session.calls) == 2

    def test_everyone_together_is_capped_too(self, monkeypatch):
        monkeypatch.setattr(address_suggest, 'LOOKUPS_PER_MINUTE', 10)
        monkeypatch.setattr(address_suggest, 'TOTAL_LOOKUPS_PER_MINUTE', 2)
        session = FakeSession(photon(feature()))

        for caller in range(4):
            suggest_addresses(f'{caller} Lillian Road', session=session, client=caller)

        assert len(session.calls) == 2

    def test_a_limited_caller_still_gets_cached_answers(self, monkeypatch):
        monkeypatch.setattr(address_suggest, 'LOOKUPS_PER_MINUTE', 1)
        session = FakeSession(photon(feature()))
        suggest_addresses('28 Lillian Road', session=session, client=1)

        assert suggest_addresses('9 Lillian Road', session=session, client=1) == []
        assert suggest_addresses('28 Lillian Road', session=session, client=1) == [
            '28 Lillian Road, Lexington, MA']
        assert len(session.calls) == 1

    def test_the_window_moves_on(self, monkeypatch):
        monkeypatch.setattr(address_suggest, 'LOOKUPS_PER_MINUTE', 1)
        now = [1000.0]
        monkeypatch.setattr(address_suggest.time, 'monotonic', lambda: now[0])
        session = FakeSession(photon(feature()))

        suggest_addresses('1 Lillian Road', session=session, client=1)
        suggest_addresses('2 Lillian Road', session=session, client=1)
        now[0] += address_suggest.RATE_WINDOW_SECONDS + 1
        suggest_addresses('3 Lillian Road', session=session, client=1)

        assert len(session.calls) == 2

    def test_a_refused_lookup_does_not_count_against_the_caller(self, monkeypatch):
        monkeypatch.setattr(address_suggest, 'LOOKUPS_PER_MINUTE', 2)
        monkeypatch.setattr(address_suggest, 'TOTAL_LOOKUPS_PER_MINUTE', 1)
        session = FakeSession(photon(feature()))
        suggest_addresses('Elm Street', session=session, client=1)  # spends the shared budget
        for n in range(3):
            suggest_addresses(f'Street {n}', session=session, client=2)  # all refused
        assert len(session.calls) == 1

        monkeypatch.setattr(address_suggest, 'TOTAL_LOOKUPS_PER_MINUTE', 100)

        # Caller 2's own allowance was never spent on the refusals.
        assert suggest_addresses('Lillian Road', session=session, client=2) == ['28 Lillian Road, Lexington, MA']
        assert len(session.calls) == 2
