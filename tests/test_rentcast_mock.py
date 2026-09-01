import pytest

from integrations.rentcast_mock import MockRentCastSession


@pytest.fixture
def session():
    return MockRentCastSession()


class TestValueEstimate:
    def test_returns_a_well_formed_avm_response(self, session):
        response = session.get(
            'https://api.rentcast.io/v1/avm/value',
            params={'address': '123 Main St, Austin, TX', 'maxRadius': 15, 'compCount': 25},
        )
        payload = response.json()

        assert response.status_code == 200
        assert response.ok is True
        assert isinstance(payload['price'], int)
        assert payload['priceRangeLow'] < payload['price'] < payload['priceRangeHigh']
        assert payload['subjectProperty']['squareFootage'] > 0
        assert len(payload['comparables']) >= 3
        for comp in payload['comparables']:
            assert comp['formattedAddress']
            assert comp['price'] > 0
            assert comp['squareFootage'] > 0

    def test_is_deterministic_for_the_same_address(self, session):
        params = {'address': '123 Main St, Austin, TX'}
        first = session.get('https://api.rentcast.io/v1/avm/value', params=params).json()
        second = session.get('https://api.rentcast.io/v1/avm/value', params=params).json()

        assert first == second

    def test_varies_between_different_addresses(self, session):
        first = session.get(
            'https://api.rentcast.io/v1/avm/value', params={'address': '123 Main St, Austin, TX'}
        ).json()
        second = session.get(
            'https://api.rentcast.io/v1/avm/value', params={'address': '456 Oak Ave, Dallas, TX'}
        ).json()

        assert first != second

    def test_comp_addresses_do_not_collide_across_different_subject_addresses(self, session):
        # Regression: comp formattedAddress used to be derived only from
        # the comp's index within the list, so every subject address's
        # first comp was the identical "100 Mock Oak St..." string.
        # RentCastClient._seed_comps_cache dedupes by normalized address,
        # so a collision like that would silently skip seeding comps for
        # every address analyzed after the first in a dev session.
        first = session.get(
            'https://api.rentcast.io/v1/avm/value', params={'address': '123 Main St, Austin, TX'}
        ).json()
        second = session.get(
            'https://api.rentcast.io/v1/avm/value', params={'address': '456 Oak Ave, Dallas, TX'}
        ).json()

        first_addresses = {comp['formattedAddress'] for comp in first['comparables']}
        second_addresses = {comp['formattedAddress'] for comp in second['comparables']}
        assert first_addresses.isdisjoint(second_addresses)

    def test_address_normalization_does_not_change_the_result(self, session):
        # Same intent as integrations.rentcast.normalize_address — casing/
        # whitespace differences shouldn't produce a different fake property.
        first = session.get(
            'https://api.rentcast.io/v1/avm/value', params={'address': '123 Main St, Austin, TX'}
        ).json()
        second = session.get(
            'https://api.rentcast.io/v1/avm/value', params={'address': '  123  main st, austin, tx  '}
        ).json()

        assert first == second

    def test_includes_at_least_one_recent_construction_comp(self, session):
        # So the new-construction reference estimate (services/market_value.py)
        # has something to show for most addresses in manual testing, rather
        # than always falling back to "not enough data".
        payload = session.get(
            'https://api.rentcast.io/v1/avm/value', params={'address': '123 Main St, Austin, TX'}
        ).json()

        assert any(comp.get('yearBuilt', 0) >= 2023 for comp in payload['comparables'])


class TestPropertyRecord:
    def test_returns_a_single_element_list_with_zoning_and_history(self, session):
        response = session.get(
            'https://api.rentcast.io/v1/properties', params={'address': '123 Main St, Austin, TX'}
        )
        payload = response.json()

        assert response.status_code == 200
        assert len(payload) == 1
        record = payload[0]
        assert record['zoning']
        assert record['subdivision']
        assert len(record['history']) == 2
        for entry in record['history'].values():
            assert entry['event'] == 'Sale'
            assert entry['price'] > 0


class TestUnknownPath:
    def test_raises_for_a_path_it_does_not_recognize(self, session):
        with pytest.raises(ValueError):
            session.get('https://api.rentcast.io/v1/listings/sale', params={'address': '123 Main St'})
