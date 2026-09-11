"""
Synthetic municipal zoning data for local development, gated behind the
same RENTCAST_MOCK switch as integrations/rentcast_mock.py (see
services.analyzer.build_municipal_zoning_lookup).

Without this, local dev would hit a city's live GIS servers on every
analysis of a supported address. Those calls are free and unmetered — the
reason mock mode exists for RentCast doesn't apply — but they'd still
mean local dev requires internet, stalls on a timeout when offline, and
leans on a government server for routine clicking around.

Deterministic per address, seeded from the same normalize_address() used
for cache keys, so revisiting an address renders the same fake parcel
rather than jittering between requests. Deliberately varies whether a
parcel is "clean" or carries teardown-blocking designations, so both the
plain and critical-warning states of the results page can be exercised
without touching the network.

Never used by the automated test suite — those inject their own stubs
directly (see tests/test_analyzer.py), keeping test assertions independent
of whatever this module happens to generate.
"""

import random

from integrations.municipal_zoning import (
    MunicipalZoningResult,
    MunicipalZoningUnavailableError,
    ZoningRestriction,
    has_jurisdiction_adapter,
)
from integrations.rentcast import normalize_address
from integrations.zoning_discovery import OFFICIAL, UNVERIFIED

MOCK_ZONING_CODES = ['SF-2', 'SF-3', 'SF-3-NP', 'SF-3-HD-NCCD-NP', 'MF-2', 'MF-3-CO-NP']

MOCK_CRITICAL = [
    ('Local Historic Districts', 'MOCKINGBIRD HEIGHTS'),
    ('National Register of Historic Districts', 'MOCKINGBIRD HEIGHTS'),
    ('City of Austin Historic Landmarks', ''),
]
MOCK_HIGH = [
    ('Neighborhood Conservation Combining District', 'MOCKINGBIRD HEIGHTS'),
    ('Residential Design Standards', 'LDC/25-2-Subchapter F'),
    ('Wildland Urban Interface 2024', ''),
    ('Waterfront Overlay', ''),
    ('Capitol View Corridors', ''),
]
MOCK_DESCRIPTIONS = [
    'SINGLE-UNIT RESIDENTIAL LOW DENSITY',
    'MULTI-UNIT RESIDENTIAL MODERATE DENSITY',
    'NEIGHBORHOOD COMMERCIAL MIXED USE',
    'URBAN RESIDENTIAL',
]

MOCK_CASE_MANAGERS = [
    ('Dana Placeholder', '(512)555-0143'),
    ('Sam Mockingbird', '(512)555-0198'),
]


def mock_lookup_municipal_zoning(address, session=None):
    """
    Drop-in stand-in for municipal_zoning.lookup_municipal_zoning. Honors
    the same jurisdiction registry, so unsupported addresses raise exactly
    as they would in production — mock mode shouldn't make every address
    look supported when most really aren't.
    """
    rng = random.Random(normalize_address(address))

    if not has_jurisdiction_adapter(address):
        # Everywhere without a curated adapter goes through nationwide
        # discovery, which finds a base zoning district for much of the
        # country but not all of it. Mirror both outcomes, including the
        # misses — a mock where every address resolves would hide how the
        # page looks for the ~1 in 4 that genuinely don't.
        if rng.random() < 0.25:
            raise MunicipalZoningUnavailableError(
                f'No zoning service discovered for {address!r}'
            )
        code = rng.choice(MOCK_ZONING_CODES)
        return MunicipalZoningResult(
            zoning_code=code,
            source='discovered',
            jurisdiction='Mockville, TX',
            zoning_description=rng.choice(MOCK_DESCRIPTIONS),
            provenance=OFFICIAL if rng.random() < 0.4 else UNVERIFIED,
            service_title='Mockville Zoning Districts',
            service_owner='mockville.gis' if rng.random() < 0.4 else 'someconsultant_corp',
            layer_name='Zoning',
            data_updated='2024-03-18',
        )

    restrictions = []
    # Roughly a third of parcels carry a demolition-blocking designation,
    # so the critical-warning path shows up regularly in local testing
    # without dominating every address.
    if rng.random() < 0.35:
        for label, detail in rng.sample(MOCK_CRITICAL, rng.randint(1, 2)):
            restrictions.append(ZoningRestriction(label=label, detail=detail, severity='critical'))

    for label, detail in rng.sample(MOCK_HIGH, rng.randint(0, 3)):
        restrictions.append(ZoningRestriction(label=label, detail=detail, severity='high'))

    if rng.random() < 0.5:
        restrictions.append(ZoningRestriction(
            label='Neighborhood Planning Areas', detail='MOCKINGBIRD HEIGHTS', severity='info',
        ))

    in_floodplain = rng.random() < 0.15
    if in_floodplain:
        restrictions.insert(0, ZoningRestriction(
            label='FEMA/City floodplain',
            detail='Parcel intersects a mapped floodplain',
            severity='critical',
        ))

    restrictions.sort(key=lambda r: {'critical': 0, 'high': 1, 'info': 2}[r.severity])

    name, phone = rng.choice(MOCK_CASE_MANAGERS)
    return MunicipalZoningResult(
        zoning_code=rng.choice(MOCK_ZONING_CODES),
        source='austin_gis_mock',
        restrictions=restrictions,
        jurisdiction='AUSTIN FULL PURPOSE',
        in_floodplain=in_floodplain,
        ordinances=[{'number': f'2010{rng.randint(1000, 9999)}-{rng.randint(10, 99)}', 'url': ''}],
        case_manager={'name': name, 'phone': phone},
    )
