"""
Saved results of on-demand searches for a zoning district's exact build
limits: setbacks, height, lot coverage, minimum lot.

Hand-checked towns and the National Zoning Atlas
(services/dimensional_standards.py) cover a small part of the country.
Everywhere else, a search reads the town's own zoning code and saves what
it found here, per (jurisdiction, district), so every analysis in that
district shows it without paying for it again.

This module is the storage and display half: which district an analysis
is in, the newest saved result for it, and that result as a table for the
results page. The searching itself, and the checks that decide which
reported numbers may be saved, arrive with it.
"""

import re
import string

from models import BuildLimitsLookup

# What can be recorded, in display order.
LIMITS = {
    'front_setback': 'Front setback',
    'side_setback': 'Side setback',
    'rear_setback': 'Rear setback',
    'max_height': 'Maximum height',
    'max_stories': 'Maximum stories',
    'max_lot_coverage': 'Maximum lot coverage',
    'max_impervious_coverage': 'Maximum impervious coverage',
    'max_floor_area_ratio': 'Maximum floor-area ratio',
    'min_lot_area': 'Minimum lot',
    'min_frontage': 'Minimum frontage',
}
UNITS = ('ft', 'sq ft', 'acres', '%', 'stories', 'ratio')


# Lot-and-bulk limits are for real districts, not free text.
_DISTRICT_OK = re.compile(r'[A-Z0-9][A-Z0-9 ./&\-]{0,29}')


# ------------------------------------------------------------- saved results

def limits_key(analysis):
    """
    ('City, ST', 'DISTRICT') to search and save under, or None when the
    analysis doesn't know both. The district is required: guessing it
    from an address would be a second thing for the search to get wrong.
    """
    detail = analysis.zoning_detail if isinstance(analysis.zoning_detail, dict) else {}
    district = ' '.join(str(detail.get('zoning_code') or analysis.property_zoning or '').split()).upper()
    if not _DISTRICT_OK.fullmatch(district):
        return None
    city, state = _split_place(detail.get('jurisdiction'))
    if not (city and state):
        city, state = _city_state(analysis.address)
    if not (city and state):
        return None
    return f'{string.capwords(city)}, {state}', district


def latest_lookup(jurisdiction, district):
    """The newest finished search for this district, or None. Failed attempts don't count."""
    return (
        BuildLimitsLookup.query.filter_by(jurisdiction=jurisdiction, district=district)
        .filter(BuildLimitsLookup.status.in_(('found', 'not_found')))
        .order_by(BuildLimitsLookup.id.desc())
        .first()
    )


def lookup_standards(row):
    """A found search as a standards dict for the results page's "What you can build" table."""
    order = list(LIMITS)
    limits = sorted((item for item in row.limits or [] if isinstance(item, dict) and item.get('key') in LIMITS),
                    key=lambda item: order.index(item['key']))
    rules = [{
        'label': LIMITS[item['key']],
        'value': _display(item['value'], item['unit']),
        'note': f'"{item["quote"]}"' + (f' ({item["section"]})' if item.get('section') else ''),
        'source_url': item.get('source_url', ''),
    } for item in limits]
    dropped = len(row.dropped or [])
    caveat = ("Found by an AI search of the town's code. Each number appears word for word in the quoted text "
              'from its source page, but no person has reviewed it: the search can pick the wrong table or an '
              'outdated copy of the code.')
    if dropped:
        caveat += (f" {dropped} other value{'s' if dropped != 1 else ''} it reported didn't match "
                   f"{'their sources and aren' if dropped != 1 else 'its source and isn'}'t shown.")
    return {
        'kind': 'ai_search',
        'jurisdiction': row.jurisdiction,
        'district': row.district,
        'district_name': '',
        'rules': rules,
        'source': {
            'citation': row.code_title or f"{row.jurisdiction}'s zoning code",
            'url': row.code_url or '',
            'as_of': f'searched {row.created_at:%Y-%m-%d}' if row.created_at else '',
        },
        'caveat': caveat,
    }


def _plain(value):
    return str(int(value)) if value == int(value) else f'{value:.6f}'.rstrip('0').rstrip('.')


def _display(value, unit):
    number = _plain(value)
    if unit == 'sq ft':
        return f'{value:,.0f} sq ft' if value == int(value) else f'{number} sq ft'
    if unit == '%':
        return f'{number}%'
    if unit == 'ratio':
        return number
    return f'{number} {unit}'


def _split_place(label):
    match = re.fullmatch(r'\s*(.+?)\s*,\s*([A-Za-z]{2})\s*', label) if isinstance(label, str) else None
    return (match.group(1), match.group(2).upper()) if match else ('', '')


def _city_state(address):
    parts = [part.strip() for part in (address or '').split(',') if part.strip()]
    if parts and parts[-1].upper() in ('USA', 'US', 'UNITED STATES'):
        parts.pop()
    if len(parts) < 3:
        return '', ''
    state = (parts[-1].split() or [''])[0]
    return (parts[-2], state.upper()) if len(state) == 2 and state.isalpha() else ('', '')
