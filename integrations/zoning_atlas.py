"""
Single-family build standards from the National Zoning Atlas, for the
states it has published as live map services.

The Atlas (zoningatlas.org) reads each town's zoning code and records, per
district, whether a one-family home is allowed and the numbers that govern
it: setbacks, height, minimum lot, coverage. For a whole state at a time
that's the only source of those numbers that can be queried by location.
Towns write them into prose and PDF tables that don't parse reliably.

It's a snapshot, though. The layer's last-updated date travels with every
answer so the page can show it, and services/dimensional_standards.py
labels the result as the Atlas's copy of the code rather than the code.

Only states whose service has been checked field by field are listed. A
state's schema isn't the same as another's (Vermont's fields are
abbreviated, New Hampshire's are spelled out), so each gets its own
mapping below.

Never raises: no answer is None, and the page just goes without numbers.
"""

import logging
from dataclasses import dataclass

import requests

from integrations.zoning_discovery import _default_session, _get, _layer_data_updated

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class AtlasLayer:
    state_name: str
    service_url: str
    layer_id: int
    parse: object  # attributes -> normalized dict, or None to skip the feature


def _vermont(attrs):
    if _text(attrs.get('OVER_')).lower() == 'yes':
        return None
    treatment = {'permitted': 'allowed', 'public hearing': 'hearing', 'prohibited': 'prohibited'}
    # 'Overlay' in the treatment column marks an overlay's row too.
    if _text(attrs.get('F1FDP')).lower() == 'overlay':
        return None
    return {
        'jurisdiction': _text(attrs.get('JXTN')),
        'district': _text(attrs.get('ABB_DIST_NAME')),
        'district_name': _text(attrs.get('DIST_NAME')),
        'single_family': treatment.get(_text(attrs.get('F1FDP')).lower(), ''),
        'min_lot_acres': _positive(attrs.get('F1F_MIN_LOT')),
        'front_ft': _positive(attrs.get('F1F_FSET')),
        'side_ft': _positive(attrs.get('F1F_SSET')),
        'rear_ft': _positive(attrs.get('F1F_RSET')),
        'frontage_ft': _positive(attrs.get('F1F_FRONT')),
        'max_height_ft': _positive(attrs.get('F1F_HEIGHT')),
        'max_stories': _positive(attrs.get('F1F_STORIES')),
        'max_lot_coverage_pct': _positive(attrs.get('F1F_MAX_LOT_BLDG')),
        'max_impervious_pct': _positive(attrs.get('F1F_MAX_LOT_IMP')),
        'far': _positive(attrs.get('F1F_FTA')),
    }


def _new_hampshire(attrs):
    if _text(attrs.get('Overlay')).lower() == 'yes' or _text(attrs.get('NonBuild')).upper() == 'Y':
        return None
    treatment = {'allowed/conditional': 'allowed', 'public hearing': 'hearing', 'prohibited': 'prohibited'}
    # New Hampshire records "is there a limit?" separately from the limit,
    # so a leftover number under a "no" doesn't get shown as a rule.
    has_height = attrs.get('Is_there_a_1_Family_Max_Height_') == 1
    has_coverage = attrs.get('Is_there_a_1_family_max_lot_cov') == 1
    has_far = attrs.get('Is_there_a_1_Family_FAR___1_yes') == 1
    return {
        'jurisdiction': _text(attrs.get('Jurisdiction')),
        'district': _text(attrs.get('Abbreviated_District_Name')),
        'district_name': _text(attrs.get('Full_District_Name')),
        'single_family': treatment.get(_text(attrs.get('F1_Family_Treatment')).lower(), ''),
        'min_lot_acres': _positive(attrs.get('F1_Family_Min_Lot__ACRES_')),
        'front_ft': _positive(attrs.get('F1_Family_Front_Setback____of_f')),
        'side_ft': _positive(attrs.get('F1_Family_Side_Setback____of_fe')),
        'rear_ft': _positive(attrs.get('F1_Family_Rear_Setback____of_fe')),
        'frontage_ft': _positive(attrs.get('F1_Family_Min_Road_Frontage____')),
        'max_height_ft': _positive(attrs.get('F1_Family_Max_Height____of_feet')) if has_height else None,
        'max_stories': _positive(attrs.get('F1_Family_Max_Height____of_stor')) if has_height else None,
        'max_lot_coverage_pct': _positive(attrs.get('F1_Family_Max_Lot_Coverage___Bu')) if has_coverage else None,
        'max_impervious_pct': _positive(attrs.get('F1_Family_Max_Lot_Coverage____1')) if has_coverage else None,
        'far': _positive(attrs.get('F1_Family_Floor_to_Area_Ratio')) if has_far else None,
    }


ATLAS_LAYERS = {
    'VT': AtlasLayer(
        state_name='Vermont',
        service_url='https://services1.arcgis.com/BkFxaEFNwHqX3tAw/arcgis/rest/services/VT_Zoning_Atlas_UVM_/FeatureServer',
        layer_id=0,
        parse=_vermont,
    ),
    'NH': AtlasLayer(
        state_name='New Hampshire',
        service_url='https://services1.arcgis.com/aguSsLS841Hp3EC4/arcgis/rest/services/NH_Zoning_Atlas_Full_Districts/FeatureServer',
        layer_id=0,
        parse=_new_hampshire,
    ),
}


def lookup_atlas_standards(state, lon, lat, session=None):
    """
    The base district's single-family standards at a point, normalized
    (see _vermont/_new_hampshire for the keys) with a 'source' citation,
    or None for a state without an Atlas service, a point outside its
    coverage, or any failure.
    """
    layer = ATLAS_LAYERS.get((state or '').upper())
    if layer is None:
        return None
    session = session or _default_session()
    layer_url = f'{layer.service_url}/{layer.layer_id}'
    try:
        payload = _get(session, f'{layer_url}/query', {
            'geometry': f'{lon},{lat}',
            'geometryType': 'esriGeometryPoint',
            'inSR': 4326,
            'spatialRel': 'esriSpatialRelIntersects',
            'outFields': '*',
            'returnGeometry': 'false',
            'f': 'json',
        })
    except (requests.RequestException, ValueError) as exc:
        logger.info('Zoning Atlas query failed for %s: %s', state, exc)
        return None
    if not isinstance(payload, dict) or 'error' in payload:
        return None

    for feature in payload.get('features') or []:
        attrs = feature.get('attributes') if isinstance(feature, dict) else None
        standards = layer.parse(attrs) if isinstance(attrs, dict) else None
        if standards and standards['district']:
            break
    else:
        return None

    updated = _layer_data_updated(session, layer.service_url, layer.layer_id)
    standards['state'] = state.upper()
    standards['source'] = {
        'citation': f'National Zoning Atlas: {layer.state_name}',
        'url': layer_url,
        'as_of': f'data last updated {updated}' if updated else 'undated snapshot',
        'data_updated': updated,
    }
    return standards


def _positive(value):
    """
    A usable number, or None. Zero is treated as missing: the Atlas uses
    it both for "no requirement" and for "not recorded", and showing a
    0 ft setback that's really a gap in the data would tell a buyer they
    can build to the lot line.
    """
    if isinstance(value, bool):
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if number > 0 else None


def _text(value):
    return value.strip() if isinstance(value, str) else ''
