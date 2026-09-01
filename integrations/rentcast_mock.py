"""
Synthetic RentCast responses for local development, gated behind the
RENTCAST_MOCK=1 env var (see services.analyzer.build_rentcast_client).

Free-tier RentCast quota (~50 calls/month) is easy to burn through just by
clicking around the app during dev — every new address costs 2 real calls.
This module plugs into RentCastClient the same way tests do (via its
`session=` constructor param) to return realistic-looking, entirely made-up
property data instead, so the UI can be exercised for any address typed in
without spending quota or touching the network at all.

Never used by the automated test suite — those use tests/test_rentcast.py's
own FakeSession/FakeResponse fixtures directly, which stay independent of
this module so a change here can't silently affect what the tests assert.
"""

import random
from datetime import datetime, timezone

from integrations.rentcast import normalize_address

# Center synthetic coordinates/comps loosely around a real-looking area so
# the results-page map and distance values render sensibly. Not tied to any
# specific real property.
MOCK_CENTER_LAT = 30.2849
MOCK_CENTER_LNG = -97.7341

MOCK_STREET_NAMES = ['Oak', 'Pine', 'Cedar', 'Maple', 'Birch', 'Elm']


class _MockResponse:
    """Just enough of requests.Response's interface for RentCastClient._get."""

    def __init__(self, payload):
        self.status_code = 200
        self.ok = True
        self._payload = payload
        self.text = str(payload)

    def json(self):
        return self._payload


class MockRentCastSession:
    """
    Drop-in replacement for requests.Session — RentCastClient only ever
    calls .get(url, params=..., headers=..., timeout=...) on it.

    Deterministic per address — seeded from integrations.rentcast's own
    normalize_address(), the same key used for cache dedup — so revisiting
    an address during a dev session renders the same fake property instead
    of jittering on every request, while still varying meaningfully
    address-to-address.
    """

    def get(self, url, params=None, headers=None, timeout=None):
        address = (params or {}).get('address', '')
        rng = random.Random(normalize_address(address))

        if url.endswith('/avm/value'):
            return _MockResponse(self._value_estimate(rng))
        if url.endswith('/properties'):
            return _MockResponse([self._property_record(rng)])
        raise ValueError(f'MockRentCastSession has no canned response for {url!r}')

    def _value_estimate(self, rng):
        this_year = datetime.now(timezone.utc).year
        sqft = rng.randint(1200, 3600)
        price = sqft * rng.randint(180, 420)
        # Vary the range width so both AVM confidence tiers show up across
        # different addresses (see services/market_value.py's TIGHT_RANGE_RATIO).
        spread = rng.uniform(0.04, 0.30)

        comparables = [self._comp(rng, i, this_year) for i in range(rng.randint(3, 8))]

        return {
            'price': price,
            'priceRangeLow': int(price * (1 - spread / 2)),
            'priceRangeHigh': int(price * (1 + spread / 2)),
            'subjectProperty': {
                'squareFootage': sqft,
                'lotSize': rng.randint(4000, 12000),
                'bedrooms': rng.randint(2, 5),
                'bathrooms': rng.choice([1, 1.5, 2, 2.5, 3]),
                'yearBuilt': rng.randint(1950, this_year - 5),
                'latitude': MOCK_CENTER_LAT + rng.uniform(-0.05, 0.05),
                'longitude': MOCK_CENTER_LNG + rng.uniform(-0.05, 0.05),
            },
            'comparables': comparables,
        }

    def _comp(self, rng, index, this_year):
        sqft = rng.randint(1200, 3600)
        # Roughly every other comp is recent new construction, so the
        # new-construction reference estimate (services/market_value.py)
        # has something to show for most addresses rather than always
        # falling back to "not enough data".
        year_built = this_year - rng.randint(0, 2) if index % 2 == 0 else rng.randint(1950, this_year - 10)
        # Street number/name drawn from the rng (seeded per subject
        # address), not just `index` — otherwise every subject address's
        # Nth comp would be the identical "<N> Mock <street>" string, and
        # RentCastClient._seed_comps_cache (which dedupes by normalized
        # address) would silently skip seeding comps for every address
        # analyzed after the first in a dev session.
        street_number = rng.randint(100, 9999)
        street_name = rng.choice(MOCK_STREET_NAMES)
        return {
            'formattedAddress': f'{street_number} Mock {street_name} St, Mockville, TX',
            'price': sqft * rng.randint(150, 450),
            'bedrooms': rng.randint(2, 5),
            'bathrooms': rng.choice([1, 1.5, 2, 2.5, 3]),
            'squareFootage': sqft,
            'lotSize': rng.randint(4000, 12000),
            'yearBuilt': year_built,
            'distance': round(rng.uniform(0.2, 14.8), 1),
            'latitude': MOCK_CENTER_LAT + rng.uniform(-0.08, 0.08),
            'longitude': MOCK_CENTER_LNG + rng.uniform(-0.08, 0.08),
        }

    def _property_record(self, rng):
        this_year = datetime.now(timezone.utc).year
        older_year = this_year - rng.randint(6, 15)
        recent_year = this_year - rng.randint(1, 4)
        return {
            'zoning': rng.choice(['R-1', 'R-2', 'SF-3', 'MF-2']),
            'subdivision': rng.choice(['Mockville Estates', 'Fake Creek Addition', 'Placeholder Heights']),
            'history': {
                f'{older_year}-03-14': {
                    'event': 'Sale',
                    'date': f'{older_year}-03-14T00:00:00.000Z',
                    'price': rng.randint(150_000, 300_000),
                },
                f'{recent_year}-09-02': {
                    'event': 'Sale',
                    'date': f'{recent_year}-09-02T00:00:00.000Z',
                    'price': rng.randint(300_000, 650_000),
                },
            },
        }
