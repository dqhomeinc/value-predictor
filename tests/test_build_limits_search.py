from types import SimpleNamespace

import pytest

from models import BuildLimitsLookup, db
from services.build_limits_search import latest_lookup, limits_key, lookup_standards

CODE_URL = 'https://ecode360.com/12345678'


def analysis(**overrides):
    fields = {'address': '123 Main St, Pittsburgh, PA 15217', 'property_zoning': None,
              'zoning_detail': {'zoning_code': 'RM-M', 'jurisdiction': 'Pittsburgh, PA'}}
    fields.update(overrides)
    return SimpleNamespace(**fields)


class TestKey:
    def test_from_discovered_zoning(self):
        assert limits_key(analysis()) == ('Pittsburgh, PA', 'RM-M')

    def test_label_without_a_state_falls_back_to_the_address(self):
        detail = {'zoning_code': 'sf-3', 'jurisdiction': 'AUSTIN FULL PURPOSE'}

        assert limits_key(analysis(address='4100 Avenue G, austin, TX 78751', zoning_detail=detail)) == ('Austin, TX', 'SF-3')

    def test_rentcast_zoning_when_there_is_no_detail(self):
        assert limits_key(analysis(zoning_detail=None, property_zoning='R-1')) == ('Pittsburgh, PA', 'R-1')

    @pytest.mark.parametrize('overrides', [
        {'zoning_detail': None},
        {'zoning_detail': {'zoning_code': ''}, 'address': '123 Main St'},
        {'zoning_detail': {'zoning_code': 'R-1; DROP TABLE'}},
        {'zoning_detail': {'zoning_code': 'x' * 40}},
    ])
    def test_none_without_a_usable_district_and_place(self, overrides):
        assert limits_key(analysis(**overrides)) is None


def saved(**overrides):
    fields = {'jurisdiction': 'Pittsburgh, PA', 'district': 'RM-M', 'status': 'found',
              'code_title': 'Pittsburgh Zoning Code', 'code_url': CODE_URL}
    fields.update(overrides)
    return BuildLimitsLookup(**fields)


def stored(key, value, unit, quote, section='Table 4.1'):
    return {'key': key, 'value': value, 'unit': unit, 'quote': quote, 'source_url': CODE_URL, 'section': section}


class TestSavedRows:
    def test_nothing_saved_yet(self, app):
        assert latest_lookup('Pittsburgh, PA', 'RM-M') is None

    def test_newest_finished_search_wins_and_failures_never_show(self, app):
        db.session.add_all([saved(status='not_found'), saved(limits=[]), saved(status='failed')])
        db.session.commit()

        assert latest_lookup('Pittsburgh, PA', 'RM-M').status == 'found'

    def test_a_found_row_becomes_the_what_you_can_build_table(self, app):
        row = saved(
            limits=[stored('min_lot_area', 10000, 'sq ft', 'Minimum lot area: 10,000 square feet'),
                    stored('front_setback', 25, 'ft', 'Minimum front yard: 25 feet'),
                    stored('max_lot_coverage', 30, '%', 'Maximum lot coverage: 30 percent')],
            dropped=[{'key': 'max_stories', 'value': 3, 'reason': 'quote not found on the page it cites'}])
        db.session.add(row)
        db.session.commit()

        standards = lookup_standards(row)

        assert standards['kind'] == 'ai_search'
        # Display order, not the order they were reported in.
        assert [rule['label'] for rule in standards['rules']] == [
            'Front setback', 'Maximum lot coverage', 'Minimum lot']
        rules = {rule['label']: rule for rule in standards['rules']}
        assert rules['Front setback']['value'] == '25 ft'
        assert rules['Maximum lot coverage']['value'] == '30%'
        assert rules['Minimum lot']['value'] == '10,000 sq ft'
        assert rules['Front setback']['note'] == '"Minimum front yard: 25 feet" (Table 4.1)'
        assert rules['Front setback']['source_url'] == CODE_URL
        assert standards['source']['citation'] == 'Pittsburgh Zoning Code'
        assert standards['source']['url'] == CODE_URL

    def test_says_how_many_values_were_dropped(self, app):
        one = saved(limits=[stored('front_setback', 25, 'ft', 'Minimum front yard: 25 feet')],
                    dropped=[{'key': 'max_stories', 'value': 3, 'reason': 'x'}])
        two = saved(limits=list(one.limits), dropped=[{'key': 'a', 'value': 1, 'reason': 'x'},
                                                      {'key': 'b', 'value': 2, 'reason': 'x'}])

        assert "1 other value it reported didn't match its source and isn't shown." in lookup_standards(one)['caveat']
        assert "2 other values it reported didn't match their sources and aren't shown." in lookup_standards(two)['caveat']
        assert 'other value' not in lookup_standards(saved(limits=list(one.limits)))['caveat']

    def test_unrecognized_keys_are_left_out(self, app):
        # A stored row can outlive a rename of the limits it holds.
        row = saved(limits=[stored('parking_spaces', 2, 'ft', 'Two parking spaces are required')])

        assert lookup_standards(row)['rules'] == []

    def test_a_row_without_a_code_title_still_names_a_source(self, app):
        assert lookup_standards(saved(code_title=''))['source']['citation'] == "Pittsburgh, PA's zoning code"
