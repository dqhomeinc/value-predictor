import pytest
from sqlalchemy.exc import IntegrityError

from integrations.rentcast import (
    RentCastClient,
    RentCastError,
    RentCastNotFoundError,
    normalize_address,
)
from models import PropertyLookupCache, db

VALUE_ESTIMATE_RESPONSE = {
    'price': 250000,
    'priceRangeLow': 240000,
    'priceRangeHigh': 260000,
    'subjectProperty': {
        'squareFootage': 1878,
        'lotSize': 5500,
        'bedrooms': 3,
        'bathrooms': 2,
        'yearBuilt': 1973,
    },
    'comparables': [{'price': 245000}, {'price': 250000}, {'price': 255000}],
}

PROPERTY_RECORD_RESPONSE = [
    {
        'zoning': 'R-1',
        'subdivision': 'Grand Lake Estates',
        'squareFootage': 1878,
    }
]


class FakeResponse:
    def __init__(self, status_code, payload=None, text=''):
        self.status_code = status_code
        self.ok = 200 <= status_code < 300
        self._payload = payload
        self.text = text or str(payload)

    def json(self):
        return self._payload


class FakeSession:
    """Records every .get() call and returns pre-programmed responses in
    order, so tests never touch the network."""

    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = []

    def get(self, url, params=None, headers=None, timeout=None):
        self.calls.append({'url': url, 'params': params, 'headers': headers})
        if not self.responses:
            raise AssertionError('FakeSession.get called more times than expected')
        return self.responses.pop(0)


def make_client(responses):
    return RentCastClient(api_key='test-key', session=FakeSession(responses))


class TestNormalizeAddress:
    def test_collapses_whitespace(self):
        assert normalize_address('123   Main   St') == '123 MAIN ST'

    def test_normalizes_comma_spacing(self):
        assert normalize_address('123 Main St,Austin,TX') == '123 MAIN ST, AUSTIN, TX'

    def test_strips_and_uppercases(self):
        assert normalize_address('  123 main st  ') == '123 MAIN ST'

    def test_equivalent_addresses_match(self):
        assert normalize_address('123 Main St, Austin, TX') == normalize_address(
            '123  main  st,austin, tx'
        )


class TestRentCastClient:
    def test_requires_api_key(self):
        with pytest.raises(ValueError):
            RentCastClient(api_key=None)

    def test_get_value_estimate_success(self):
        client = make_client([FakeResponse(200, VALUE_ESTIMATE_RESPONSE)])
        result = client.get_value_estimate('123 Main St, Austin, TX')
        assert result['price'] == 250000
        assert client.session.calls[0]['url'].endswith('/avm/value')
        assert client.session.calls[0]['headers']['X-Api-Key'] == 'test-key'

    def test_get_value_estimate_requests_wide_comps_by_default(self):
        # So the nearby-sales radius filter (5/10/15 mi) has enough comps
        # to filter client-side without a second call.
        client = make_client([FakeResponse(200, VALUE_ESTIMATE_RESPONSE)])
        client.get_value_estimate('123 Main St, Austin, TX')
        params = client.session.calls[0]['params']
        assert params['maxRadius'] == 15
        assert params['compCount'] == 25

    def test_get_property_record_success(self):
        client = make_client([FakeResponse(200, PROPERTY_RECORD_RESPONSE)])
        result = client.get_property_record('123 Main St, Austin, TX')
        assert result['zoning'] == 'R-1'
        assert client.session.calls[0]['url'].endswith('/properties')

    def test_get_property_record_empty_list_raises_not_found(self):
        client = make_client([FakeResponse(200, [])])
        with pytest.raises(RentCastNotFoundError):
            client.get_property_record('1 Nowhere Rd')

    def test_404_raises_not_found(self):
        client = make_client([FakeResponse(404, None, text='not found')])
        with pytest.raises(RentCastNotFoundError):
            client.get_value_estimate('1 Nowhere Rd')

    def test_server_error_raises_rentcast_error(self):
        client = make_client([FakeResponse(500, None, text='boom')])
        with pytest.raises(RentCastError):
            client.get_value_estimate('123 Main St')


class TestLookupProperty:
    def test_cache_miss_makes_two_calls_and_caches(self, app):
        client = make_client([
            FakeResponse(200, VALUE_ESTIMATE_RESPONSE),
            FakeResponse(200, PROPERTY_RECORD_RESPONSE),
        ])

        avm_json, property_json, from_cache, source = client.lookup_property('123 Main St, Austin, TX')

        assert from_cache is False
        assert source == 'full_lookup'
        assert avm_json['price'] == 250000
        assert property_json['zoning'] == 'R-1'
        assert len(client.session.calls) == 2

        row = PropertyLookupCache.query.filter_by(
            normalized_address='123 MAIN ST, AUSTIN, TX'
        ).first()
        assert row is not None
        assert row.source == 'full_lookup'
        assert row.raw_avm_json['price'] == 250000
        assert row.raw_property_json['zoning'] == 'R-1'

    def test_cache_hit_makes_zero_calls(self, app):
        PropertyLookupCache.query.session.add(PropertyLookupCache(
            normalized_address='123 MAIN ST, AUSTIN, TX',
            raw_avm_json=VALUE_ESTIMATE_RESPONSE,
            raw_property_json=PROPERTY_RECORD_RESPONSE[0],
        ))
        PropertyLookupCache.query.session.commit()

        client = make_client([])  # no responses queued — a live call would raise
        avm_json, property_json, from_cache, source = client.lookup_property(
            '123  main st, austin, tx'
        )

        assert from_cache is True
        assert source == 'full_lookup'
        assert avm_json['price'] == 250000
        assert property_json['zoning'] == 'R-1'
        assert client.session.calls == []

    def test_repeated_lookups_of_same_address_stay_at_two_calls(self, app):
        client = make_client([
            FakeResponse(200, VALUE_ESTIMATE_RESPONSE),
            FakeResponse(200, PROPERTY_RECORD_RESPONSE),
        ])

        client.lookup_property('123 Main St, Austin, TX')
        for _ in range(4):
            client.lookup_property('123 Main St, Austin, TX')

        assert len(client.session.calls) == 2

    def test_concurrent_cache_miss_recovers_from_integrity_error(self, app, monkeypatch):
        """
        Two requests for the same new address can both pass the cache-miss
        check before either commits. The loser's commit() must not blow up
        with an unhandled IntegrityError — it should recover by reusing the
        row the winner already wrote.
        """
        client = make_client([
            FakeResponse(200, VALUE_ESTIMATE_RESPONSE),
            FakeResponse(200, PROPERTY_RECORD_RESPONSE),
        ])

        real_commit = db.session.commit

        def racing_commit():
            # Simulate a concurrent request winning the insert race: it
            # commits the same normalized_address first, so our own pending
            # insert now violates the unique constraint.
            db.session.rollback()
            db.session.add(PropertyLookupCache(
                normalized_address='123 MAIN ST, AUSTIN, TX',
                raw_avm_json=VALUE_ESTIMATE_RESPONSE,
                raw_property_json=PROPERTY_RECORD_RESPONSE[0],
            ))
            real_commit()
            monkeypatch.setattr(db.session, 'commit', real_commit)  # only race once
            raise IntegrityError('simulated concurrent insert', None, Exception('UNIQUE constraint failed'))

        monkeypatch.setattr(db.session, 'commit', racing_commit)

        avm_json, property_json, from_cache, source = client.lookup_property('123 Main St, Austin, TX')

        # Both live calls still happened — the race is only discovered at
        # commit time — but the request recovers instead of 500ing.
        assert len(client.session.calls) == 2
        assert from_cache is True
        assert source == 'full_lookup'
        assert avm_json == VALUE_ESTIMATE_RESPONSE
        assert property_json == PROPERTY_RECORD_RESPONSE[0]

        # Only one row ever lands in the cache, not two.
        assert PropertyLookupCache.query.filter_by(
            normalized_address='123 MAIN ST, AUSTIN, TX'
        ).count() == 1


VALUE_ESTIMATE_WITH_ADDRESSED_COMPS = {
    'price': 250000,
    'priceRangeLow': 240000,
    'priceRangeHigh': 260000,
    'subjectProperty': {'squareFootage': 1878, 'bedrooms': 3, 'bathrooms': 2, 'yearBuilt': 1973},
    'comparables': [
        {
            'formattedAddress': '456 Oak Ave, Austin, TX',
            'price': 245000,
            'squareFootage': 1800,
            'bedrooms': 3,
            'bathrooms': 2,
            'yearBuilt': 1968,
            'lotSize': 5000,
            'latitude': 30.1,
            'longitude': -97.1,
        },
        {
            'formattedAddress': '789 Pine Ln, Austin, TX',
            'price': 260000,
            'squareFootage': 1950,
            'bedrooms': 4,
            'bathrooms': 2.5,
        },
        {'price': 255000},  # no formattedAddress — must not be seeded
    ],
}


class TestCompSeeding:
    def test_seeds_addressed_comps_for_free_on_a_real_lookup(self, app):
        client = make_client([
            FakeResponse(200, VALUE_ESTIMATE_WITH_ADDRESSED_COMPS),
            FakeResponse(200, PROPERTY_RECORD_RESPONSE),
        ])

        client.lookup_property('123 Main St, Austin, TX')

        # No extra HTTP calls for seeding — still exactly the 2 for the
        # subject address itself.
        assert len(client.session.calls) == 2

        oak = PropertyLookupCache.query.filter_by(normalized_address='456 OAK AVE, AUSTIN, TX').first()
        assert oak is not None
        assert oak.source == 'comp_seed'
        assert oak.raw_avm_json['price'] == 245000
        assert oak.raw_avm_json['subjectProperty']['squareFootage'] == 1800
        assert oak.raw_avm_json['subjectProperty']['bedrooms'] == 3
        assert oak.raw_avm_json['comparables'] == []
        assert oak.raw_property_json == {'zoning': None, 'subdivision': None, 'history': None}

        pine = PropertyLookupCache.query.filter_by(normalized_address='789 PINE LN, AUSTIN, TX').first()
        assert pine is not None
        assert pine.source == 'comp_seed'

    def test_comp_without_address_is_not_seeded(self, app):
        client = make_client([
            FakeResponse(200, VALUE_ESTIMATE_WITH_ADDRESSED_COMPS),
            FakeResponse(200, PROPERTY_RECORD_RESPONSE),
        ])

        client.lookup_property('123 Main St, Austin, TX')

        # subject + 2 addressed comps = 3 rows; the addressless comp seeds nothing.
        assert PropertyLookupCache.query.count() == 3

    def test_does_not_overwrite_an_existing_full_lookup_row(self, app):
        # 456 Oak Ave was already fully looked up in its own right — a
        # comp sighting of it elsewhere must not clobber that real data.
        db.session.add(PropertyLookupCache(
            normalized_address='456 OAK AVE, AUSTIN, TX',
            source='full_lookup',
            raw_avm_json={'price': 999999, 'subjectProperty': {}, 'comparables': []},
            raw_property_json={'zoning': 'R-2', 'subdivision': 'Real Data', 'history': None},
        ))
        db.session.commit()

        client = make_client([
            FakeResponse(200, VALUE_ESTIMATE_WITH_ADDRESSED_COMPS),
            FakeResponse(200, PROPERTY_RECORD_RESPONSE),
        ])
        client.lookup_property('123 Main St, Austin, TX')

        oak = PropertyLookupCache.query.filter_by(normalized_address='456 OAK AVE, AUSTIN, TX').first()
        assert oak.source == 'full_lookup'
        assert oak.raw_avm_json['price'] == 999999
        assert oak.raw_property_json['subdivision'] == 'Real Data'

    def test_cache_hit_does_not_reseed(self, app):
        # Pre-existing cache hit for the subject address — lookup_property
        # returns early and never even sees the comps, so nothing new
        # should be seeded.
        db.session.add(PropertyLookupCache(
            normalized_address='123 MAIN ST, AUSTIN, TX',
            source='full_lookup',
            raw_avm_json=VALUE_ESTIMATE_WITH_ADDRESSED_COMPS,
            raw_property_json=PROPERTY_RECORD_RESPONSE[0],
        ))
        db.session.commit()

        client = make_client([])  # a live call would raise
        client.lookup_property('123 Main St, Austin, TX')

        assert PropertyLookupCache.query.count() == 1


class TestForceRefresh:
    def test_force_refresh_spends_two_calls_despite_existing_cache_entry(self, app):
        db.session.add(PropertyLookupCache(
            normalized_address='456 OAK AVE, AUSTIN, TX',
            source='comp_seed',
            raw_avm_json={'price': 245000, 'subjectProperty': {}, 'comparables': []},
            raw_property_json={'zoning': None, 'subdivision': None, 'history': None},
        ))
        db.session.commit()

        client = make_client([
            FakeResponse(200, VALUE_ESTIMATE_RESPONSE),
            FakeResponse(200, PROPERTY_RECORD_RESPONSE),
        ])

        avm_json, property_json, from_cache, source = client.lookup_property(
            '456 Oak Ave, Austin, TX', force_refresh=True
        )

        assert len(client.session.calls) == 2
        assert from_cache is False
        assert source == 'full_lookup'
        assert avm_json['price'] == 250000

        # The existing row was upgraded in place, not duplicated.
        assert PropertyLookupCache.query.filter_by(normalized_address='456 OAK AVE, AUSTIN, TX').count() == 1
        row = PropertyLookupCache.query.filter_by(normalized_address='456 OAK AVE, AUSTIN, TX').first()
        assert row.source == 'full_lookup'
        assert row.raw_property_json['zoning'] == 'R-1'

    def test_force_refresh_on_a_never_seen_address_behaves_like_a_normal_miss(self, app):
        client = make_client([
            FakeResponse(200, VALUE_ESTIMATE_RESPONSE),
            FakeResponse(200, PROPERTY_RECORD_RESPONSE),
        ])

        avm_json, property_json, from_cache, source = client.lookup_property(
            '123 Main St, Austin, TX', force_refresh=True
        )

        assert len(client.session.calls) == 2
        assert from_cache is False
        assert source == 'full_lookup'

    def test_force_refresh_is_ignored_for_an_existing_full_lookup_row(self, app):
        # Guards against any POST to /analyses (a normal new-analysis
        # submission, or a resubmit of an address already fully looked
        # up) forcing 2 real paid RentCast calls just by including
        # force_refresh=1 — that flag should only ever upgrade a partial
        # comp_seed row, never bypass a real cache entry.
        db.session.add(PropertyLookupCache(
            normalized_address='123 MAIN ST, AUSTIN, TX',
            source='full_lookup',
            raw_avm_json=VALUE_ESTIMATE_RESPONSE,
            raw_property_json=PROPERTY_RECORD_RESPONSE[0],
        ))
        db.session.commit()

        client = make_client([])  # a live call would raise

        avm_json, _property_json, from_cache, source = client.lookup_property(
            '123 Main St, Austin, TX', force_refresh=True
        )

        assert len(client.session.calls) == 0
        assert from_cache is True
        assert source == 'full_lookup'
        assert avm_json['price'] == VALUE_ESTIMATE_RESPONSE['price']
