"""
Exact build standards for the zoning district a parcel sits in: how far a
house has to stay from each lot line, how tall it can be, the minimum lot,
and, where the town sets one, how much floor area a new home can have.

Towns write these into their own zoning codes as text, so there's no
national source to query. The numbers here are checked by hand against
each town's own code (CURATED below), and the page cites the section and
says how current it is. Adding a town means reading its code, not wiring
up a service.

Where a parcel isn't covered, the page keeps the plain-English
explanations from services/zoning_guidance.py, which deliberately state
no numbers at all.

Informational only, like the rest of zoning: never fed into the deal math.
"""

from dataclasses import dataclass


@dataclass(frozen=True)
class FloorAreaBand:
    """One row of a floor-area table: cap = base + rate * (lot - lower)."""
    lower_sqft: float
    base_sqft: float
    rate: float


def max_floor_area(bands, lot_sqft):
    """The floor-area cap for a lot, or None when the lot size isn't known."""
    if not isinstance(lot_sqft, (int, float)) or lot_sqft <= 0:
        return None
    band = max((b for b in bands if lot_sqft >= b.lower_sqft), key=lambda b: b.lower_sqft)
    return band.base_sqft + band.rate * (lot_sqft - band.lower_sqft)


LEXINGTON_SOURCE = {
    'citation': 'Lexington Zoning Bylaw, Ch. 135: Table 2 (Schedule of Dimensional Controls), §4.3.5 and §4.4.2',
    'url': 'https://lexingtonma.gov/DocumentCenter/View/7684/Zoning-Bylaw-PDF',
    'as_of': 'amendments through the 2025 Annual Town Meeting',
}

# Table 4.4.2.2, the limit for new homes. §4.4.2(a) says demolishing more
# than half of a house's shell doesn't count as altering it, so a teardown
# is new construction and lands here, not on the looser Table 4.4.2.1 for
# homes permitted before 2024. Each band starts where the previous one
# ends, which the tests check to catch a mistyped number.
LEXINGTON_NEW_HOME_FLOOR_AREA = (
    FloorAreaBand(0, 0, 0.76),
    FloorAreaBand(5_000, 3_800, 0.42),
    FloorAreaBand(7_500, 4_850, 0.12),
    FloorAreaBand(10_000, 5_150, 0.11),
    FloorAreaBand(15_000, 5_700, 0.10),
    FloorAreaBand(30_000, 7_200, 0.10),
)


def _lexington_residential(name, min_lot_sqft, frontage_ft):
    # Table 2 gives RO, RS and RT the same yards and height. Lot size and
    # frontage are what differ.
    return {
        'district_name': name,
        'min_lot_sqft': min_lot_sqft,
        'frontage_ft': frontage_ft,
        'front_ft': 30,
        'side_ft': 15,
        'rear_ft': 15,
        'max_stories': 2.5,
        'max_height_ft': 40,
        'floor_area': LEXINGTON_NEW_HOME_FLOOR_AREA,
    }


# Keyed by (CITY, STATE) as discovery labels a jurisdiction. Residential
# districts only: this app is about rebuilding homes, and commercial
# columns in these tables carry exceptions that aren't worth transcribing
# until someone needs them.
CURATED = {
    ('LEXINGTON', 'MA'): {
        'source': LEXINGTON_SOURCE,
        'districts': {
            'RO': _lexington_residential('One Family Dwelling', 30_000, 150),
            'RS': _lexington_residential('One Family Dwelling', 15_500, 125),
            'RT': _lexington_residential('Two Family Dwelling', 15_500, 125),
        },
    },
}


def standards_for(detail, lot_sqft=None):
    """
    Render-ready standards for a stored zoning detail, or None where no
    source covers it. Applied at render time, so a corrected number
    reaches past analyses too.
    """
    if not isinstance(detail, dict):
        return None
    return curated_standards(detail.get('jurisdiction'), detail.get('zoning_code'), lot_sqft)


def curated_standards(jurisdiction, district, lot_sqft=None):
    """Standards for a district in a curated town, or None if it isn't one."""
    key = _jurisdiction_key(jurisdiction)
    town = CURATED.get(key) if key else None
    code = (district or '').strip().upper()
    data = town['districts'].get(code) if town else None
    if not data:
        return None

    front = data['front_ft']
    rules = [
        _rule('Front setback', f'{front} ft',
              'Measured from the front lot line, which usually sits behind the sidewalk. '
              f'On a corner lot, {round(front * 2 / 3)} ft from the second street.'),
        _rule('Side setback', f"{data['side_ft']} ft",
              'Narrower nonconforming lots can qualify for less (§8.4.1). Confirm with the Building Department.'),
        _rule('Rear setback', f"{data['rear_ft']} ft", ''),
        _rule('Maximum height', f"{_number(data['max_stories'])} stories / {data['max_height_ft']} ft",
              "Also no taller than 20 ft plus 4/3 of the house's closest distance to a lot line (§4.3.5): "
              'exactly 40 ft at the 15 ft side setback, lower if closer. Near a street, it also can\'t rise '
              "above the street's grade by more than its distance to the street's centerline (Table 2, note h)."),
        _floor_area_rule(data['floor_area'], lot_sqft),
        _rule('Minimum lot', f"{data['min_lot_sqft']:,} sq ft with {data['frontage_ft']} ft of frontage", ''),
    ]
    return {
        'kind': 'bylaw',
        'jurisdiction': jurisdiction,
        'district': code,
        'district_name': data['district_name'],
        'rules': rules,
        'source': town['source'],
        'caveat': '',
    }


def _floor_area_rule(bands, lot_sqft):
    note = ('Lexington counts demolishing more than half of a house as new construction (§4.4.2), '
            'so a teardown gets the new-home limit.')
    cap = max_floor_area(bands, lot_sqft)
    if cap is None:
        return _rule('Max floor area, new home', 'Set by lot size',
                     f'{note} The lot size wasn\'t available to work it out.')
    return _rule('Max floor area, new home', f'{cap:,.0f} sq ft', f'On this {lot_sqft:,.0f} sq ft lot. {note}')


def _jurisdiction_key(label):
    if not isinstance(label, str) or ',' not in label:
        return None
    city, state = label.rsplit(',', 1)
    return city.strip().upper(), state.strip().upper()


def _number(value):
    """30 -> '30', 2.5 -> '2.5', '1' -> '1'. Keeps the page free of '30.0'."""
    try:
        number = float(value)
    except (TypeError, ValueError):
        return str(value)
    return f'{number:g}'


def _rule(label, value, note):
    return {'label': label, 'value': value, 'note': note}
