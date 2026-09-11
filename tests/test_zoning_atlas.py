import pytest
import requests

from integrations.zoning_atlas import ATLAS_LAYERS, lookup_atlas_standards


class FakeResponse:
    def __init__(self, payload):
        self._payload = payload

    def raise_for_status(self):
        pass

    def json(self):
        if isinstance(self._payload, Exception):
            raise self._payload
        return self._payload


class FakeSession:
    """Answers the point query and the layer-metadata call separately."""

    def __init__(self, query, meta=None):
        self.query = query
        self.meta = meta if meta is not None else {'editingInfo': {'dataLastEditDate': 1710201600000}}
        self.calls = []

    def get(self, url, params=None, timeout=None, headers=None):
        self.calls.append(url)
        payload = self.query if url.endswith('/query') else self.meta
        if isinstance(payload, requests.RequestException):
            raise payload
        return FakeResponse(payload)


def features(*attrs):
    return {'features': [{'attributes': a} for a in attrs]}


# Attributes as the live New Hampshire service returned them for Concord City Hall.
NH_CONCORD = {
    'Jurisdiction': 'Concord', 'Abbreviated_District_Name': 'CVP', 'Full_District_Name': 'Civic Performance',
    'Overlay': 'No', 'NonBuild': 'N', 'F1_Family_Treatment': 'Public Hearing',
    'F1_Family_Min_Lot__ACRES_': None,
    'F1_Family_Front_Setback____of_f': 15, 'F1_Family_Side_Setback____of_fe': 15,
    'F1_Family_Rear_Setback____of_fe': 15, 'F1_Family_Min_Road_Frontage____': 80,
    'Is_there_a_1_Family_Max_Height_': 1, 'F1_Family_Max_Height____of_feet': 45,
    'F1_Family_Max_Height____of_stor': None,
    'Is_there_a_1_family_max_lot_cov': 1, 'F1_Family_Max_Lot_Coverage___Bu': None,
    'F1_Family_Max_Lot_Coverage____1': 80,
    'Is_there_a_1_Family_FAR___1_yes': 0, 'F1_Family_Floor_to_Area_Ratio': 2,
}

# Attributes as the live Vermont service returned them for 94 Main St, Brattleboro.
VT_BRATTLEBORO = {
    'JXTN': 'Brattleboro', 'DIST_NAME': 'Urban Center', 'ABB_DIST_NAME': 'UC', 'OVER_': 'No',
    'F1FDP': 'Public Hearing', 'F1F_MIN_LOT': 0.05, 'F1F_FSET': 0, 'F1F_SSET': 0, 'F1F_RSET': 0,
    'F1F_FRONT': 30, 'F1F_STORIES': 6, 'F1F_HEIGHT': None,
}


class TestNewHampshire:
    def test_maps_the_fields(self):
        result = lookup_atlas_standards('NH', -71.54, 43.2, session=FakeSession(features(NH_CONCORD)))

        assert result['jurisdiction'] == 'Concord'
        assert result['district'] == 'CVP'
        assert result['district_name'] == 'Civic Performance'
        assert result['single_family'] == 'hearing'
        assert (result['front_ft'], result['side_ft'], result['rear_ft']) == (15, 15, 15)
        assert result['frontage_ft'] == 80
        assert result['max_height_ft'] == 45
        assert result['max_impervious_pct'] == 80
        assert result['min_lot_acres'] is None

    def test_a_number_under_a_no_flag_is_not_a_rule(self):
        # FAR flag is 0 but the value field holds 2.
        result = lookup_atlas_standards('NH', -71.54, 43.2, session=FakeSession(features(NH_CONCORD)))

        assert result['far'] is None

    def test_cites_the_layer_and_its_date(self):
        result = lookup_atlas_standards('nh', -71.54, 43.2, session=FakeSession(features(NH_CONCORD)))

        assert result['state'] == 'NH'
        assert result['source']['citation'] == 'National Zoning Atlas: New Hampshire'
        assert result['source']['url'] == f"{ATLAS_LAYERS['NH'].service_url}/0"
        assert result['source']['as_of'] == 'data last updated 2024-03-12'

    def test_undated_layer_says_so(self):
        session = FakeSession(features(NH_CONCORD), meta={})

        assert lookup_atlas_standards('NH', 0, 0, session=session)['source']['as_of'] == 'undated snapshot'

    def test_skips_overlays_and_unbuildable_areas_for_the_base_district(self):
        overlay = {**NH_CONCORD, 'Overlay': 'Yes', 'Abbreviated_District_Name': 'HIST'}
        water = {**NH_CONCORD, 'NonBuild': 'Y', 'Abbreviated_District_Name': 'WATER'}

        result = lookup_atlas_standards('NH', 0, 0, session=FakeSession(features(overlay, water, NH_CONCORD)))

        assert result['district'] == 'CVP'

    def test_only_overlays_is_none(self):
        overlay = {**NH_CONCORD, 'Overlay': 'Yes'}

        assert lookup_atlas_standards('NH', 0, 0, session=FakeSession(features(overlay))) is None


class TestVermont:
    def test_maps_the_fields(self):
        result = lookup_atlas_standards('VT', -72.56, 42.85, session=FakeSession(features(VT_BRATTLEBORO)))

        assert result['district'] == 'UC'
        assert result['single_family'] == 'hearing'
        assert result['min_lot_acres'] == 0.05
        assert result['frontage_ft'] == 30
        assert result['max_stories'] == 6
        assert result['source']['citation'] == 'National Zoning Atlas: Vermont'

    def test_zero_setbacks_are_treated_as_missing(self):
        # A 0 could be "none required" or "not recorded". Showing 0 ft
        # would tell a buyer they can build to the lot line.
        result = lookup_atlas_standards('VT', 0, 0, session=FakeSession(features(VT_BRATTLEBORO)))

        assert (result['front_ft'], result['side_ft'], result['rear_ft']) == (None, None, None)

    @pytest.mark.parametrize('treatment, expected', [
        ('Permitted', 'allowed'), ('Prohibited', 'prohibited'), ('None', ''), (None, ''),
    ])
    def test_treatment(self, treatment, expected):
        attrs = {**VT_BRATTLEBORO, 'F1FDP': treatment}

        assert lookup_atlas_standards('VT', 0, 0, session=FakeSession(features(attrs)))['single_family'] == expected

    def test_overlay_rows_are_skipped(self):
        attrs = {**VT_BRATTLEBORO, 'F1FDP': 'Overlay'}

        assert lookup_atlas_standards('VT', 0, 0, session=FakeSession(features(attrs))) is None


class TestNoAnswer:
    def test_state_without_an_atlas_service_makes_no_call(self):
        session = FakeSession(features(NH_CONCORD))

        assert lookup_atlas_standards('MA', 0, 0, session=session) is None
        assert lookup_atlas_standards(None, 0, 0, session=session) is None
        assert session.calls == []

    @pytest.mark.parametrize('query', [
        {'features': []},
        {'error': {'code': 400}},
        requests.ConnectionError('down'),
        ValueError('not json'),
        [],
        {'features': [{'attributes': None}]},
    ])
    def test_never_raises(self, query):
        assert lookup_atlas_standards('NH', 0, 0, session=FakeSession(query)) is None

    def test_feature_without_a_district_name_is_skipped(self):
        attrs = {**NH_CONCORD, 'Abbreviated_District_Name': '  '}

        assert lookup_atlas_standards('NH', 0, 0, session=FakeSession(features(attrs))) is None
