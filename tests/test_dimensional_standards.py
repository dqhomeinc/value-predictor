from itertools import pairwise

import pytest

from services.dimensional_standards import (
    CURATED,
    LEXINGTON_NEW_HOME_FLOOR_AREA,
    curated_standards,
    max_floor_area,
    standards_for,
)


def rules_by_label(standards):
    return {rule['label']: rule for rule in standards['rules']}


class TestLexingtonMatchesItsBylaw:
    """Values transcribed from Lexington Zoning Bylaw Ch. 135, Table 2 and
    §4.4.2 (amendments through the 2025 Annual Town Meeting). A failure
    here means a number drifted from the source, not a style problem."""

    def test_rs_yards_and_height(self):
        rules = rules_by_label(curated_standards('Lexington, MA', 'RS'))

        assert rules['Front setback']['value'] == '30 ft'
        assert rules['Side setback']['value'] == '15 ft'
        assert rules['Rear setback']['value'] == '15 ft'
        assert rules['Maximum height']['value'] == '2.5 stories / 40 ft'
        assert rules['Minimum lot']['value'] == '15,500 sq ft with 125 ft of frontage'

    def test_corner_lot_second_street_is_two_thirds(self):
        # Table 2, note b.
        note = rules_by_label(curated_standards('Lexington, MA', 'RS'))['Front setback']['note']

        assert '20 ft from the second street' in note

    def test_height_notes_cite_the_near_lot_line_formula(self):
        note = rules_by_label(curated_standards('Lexington, MA', 'RS'))['Maximum height']['note']

        assert '4/3' in note and '§4.3.5' in note

    def test_ro_has_larger_lot_and_frontage(self):
        rules = rules_by_label(curated_standards('Lexington, MA', 'RO'))

        assert rules['Minimum lot']['value'] == '30,000 sq ft with 150 ft of frontage'
        assert rules['Front setback']['value'] == '30 ft'

    def test_district_names(self):
        assert curated_standards('Lexington, MA', 'RS')['district_name'] == 'One Family Dwelling'
        assert curated_standards('Lexington, MA', 'RO')['district_name'] == 'One Family Dwelling'
        assert curated_standards('Lexington, MA', 'RT')['district_name'] == 'Two Family Dwelling'


class TestNewHomeFloorArea:
    @pytest.mark.parametrize('lot, expected', [
        (4_000, 3_040),       # 0.76 * lot
        (5_000, 3_800),
        (7_500, 4_850),
        (10_000, 5_150),
        (14_404, 5_634.44),   # the 28 Lillian Rd parcel
        (15_000, 5_700),
        (30_000, 7_200),
        (40_000, 8_200),
    ])
    def test_table_4_4_2_2(self, lot, expected):
        assert max_floor_area(LEXINGTON_NEW_HOME_FLOOR_AREA, lot) == pytest.approx(expected)

    def test_bands_meet_at_every_boundary(self):
        # Each band's base is where the previous band ends. A mistyped base
        # or rate breaks this, so it's a cheap check on the transcription.
        bands = LEXINGTON_NEW_HOME_FLOOR_AREA
        for previous, band in pairwise(bands):
            end_of_previous = previous.base_sqft + previous.rate * (band.lower_sqft - previous.lower_sqft)
            assert end_of_previous == pytest.approx(band.base_sqft)

    @pytest.mark.parametrize('lot', [None, 0, -5, 'big'])
    def test_unknown_lot_size_is_none(self, lot):
        assert max_floor_area(LEXINGTON_NEW_HOME_FLOOR_AREA, lot) is None

    def test_rule_shows_the_cap_for_this_lot(self):
        rule = rules_by_label(curated_standards('Lexington, MA', 'RS', lot_sqft=14_404))['Max floor area, new home']

        assert rule['value'] == '5,634 sq ft'
        assert 'On this 14,404 sq ft lot' in rule['note']
        assert 'teardown' in rule['note']

    def test_rule_without_a_lot_size_says_so(self):
        rule = rules_by_label(curated_standards('Lexington, MA', 'RS'))['Max floor area, new home']

        assert rule['value'] == 'Set by lot size'


class TestCuratedLookup:
    def test_case_and_spacing_insensitive(self):
        assert curated_standards(' lexington , ma ', ' rs ')['district'] == 'RS'

    @pytest.mark.parametrize('jurisdiction, district', [
        ('Lexington, KY', 'RS'),             # same town name, different state
        ('Lexington, MA', 'CM'),             # commercial, not curated
        ('AUSTIN FULL PURPOSE', 'SF-3'),     # curated adapter's label, no state
        ('Pittsburgh, PA', 'RM-M'),
        (None, None),
        ('', ''),
    ])
    def test_uncovered_is_none(self, jurisdiction, district):
        assert curated_standards(jurisdiction, district) is None

    def test_every_curated_town_is_cited(self):
        for town in CURATED.values():
            source = town['source']
            assert source['citation']
            assert source['url'].startswith('https://')
            assert source['as_of']


class TestStandardsFor:
    def test_uses_the_town_code(self):
        detail = {'jurisdiction': 'Lexington, MA', 'zoning_code': 'RS'}

        assert standards_for(detail, lot_sqft=14_404)['kind'] == 'bylaw'

    @pytest.mark.parametrize('detail', [None, 'junk', {}, {'jurisdiction': 'Pittsburgh, PA', 'zoning_code': 'RM-M'}])
    def test_none_when_nothing_covers_it(self, detail):
        assert standards_for(detail) is None
