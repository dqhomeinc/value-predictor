"""
RentCast API client.

Wraps the two RentCast endpoints this app needs — Value Estimate (AVM) and
Property Records — behind an indefinite, cache-check-first lookup. RentCast
bills a flat 1 request per endpoint hit regardless of response size, and its
free tier is ~50 requests/month, so `lookup_property` is written to spend
the minimum possible: 0 calls on a cache hit, 2 on a miss (never re-fetched
afterward, since RentCast's terms place no limit on how long results may be
cached).

Reference: https://developers.rentcast.io/reference
"""

import logging
import re

import requests
from sqlalchemy.exc import IntegrityError

from models import PropertyLookupCache, db

logger = logging.getLogger(__name__)

RENTCAST_BASE_URL = 'https://api.rentcast.io/v1'
DEFAULT_TIMEOUT = 10  # seconds

# Widest radius the results page's nearby-sales filter offers (5/10/15 mi).
# Requesting comps out to this distance up front means the 5/10/15 mi
# filter can run entirely client-side against one cached fetch.
NEARBY_SALES_MAX_RADIUS_MILES = 15


class RentCastError(Exception):
    """Base class for RentCast client failures."""


class RentCastNotFoundError(RentCastError):
    """RentCast has no data for the given address."""


def normalize_address(address):
    """
    Collapse whitespace/casing so equivalent address strings ("123 Main St"
    vs "123  main st") share the same property_lookup_cache row.

    This is a display-string normalization only — it does not validate or
    geocode the address. RentCast does its own address matching server-side.
    """
    collapsed = ' '.join(address.strip().split())
    # Normalize comma spacing (e.g. "Main St,Austin, TX" -> "Main St, Austin, TX")
    collapsed = re.sub(r'\s*,\s*', ', ', collapsed)
    return collapsed.upper()


class RentCastClient:
    """
    Usage:
        client = RentCastClient(api_key=os.environ['RENTCAST_API_KEY'])
        avm_json, property_json, from_cache, source = client.lookup_property(address)
    """

    def __init__(self, api_key, session=None, base_url=RENTCAST_BASE_URL, timeout=DEFAULT_TIMEOUT):
        if not api_key:
            raise ValueError('RentCast API key is required')
        self.api_key = api_key
        self.session = session or requests.Session()
        self.base_url = base_url
        self.timeout = timeout

    def _get(self, path, params):
        url = f'{self.base_url}{path}'
        logger.info('RentCast API call: GET %s params=%s', path, params)
        try:
            response = self.session.get(
                url,
                params=params,
                headers={'X-Api-Key': self.api_key, 'Accept': 'application/json'},
                timeout=self.timeout,
            )
        except requests.RequestException as exc:
            raise RentCastError(f'RentCast request to {path} failed: {exc}') from exc

        if response.status_code == 404:
            raise RentCastNotFoundError(f'No RentCast data found for {path} params={params}')
        if not response.ok:
            raise RentCastError(
                f'RentCast request to {path} failed ({response.status_code}): {response.text[:300]}'
            )
        return response.json()

    def get_value_estimate(self, address, max_radius=NEARBY_SALES_MAX_RADIUS_MILES, comp_count=25):
        """
        Value Estimate (AVM) endpoint. Returns `price`, `priceRangeLow`/
        `priceRangeHigh`, `subjectProperty` (bedrooms, bathrooms,
        squareFootage, lotSize, yearBuilt, ...), and `comparables`
        (formattedAddress, price, bedrooms, bathrooms, squareFootage,
        distance, ...) — all in one call. https://developers.rentcast.io/reference/value-estimate

        max_radius/comp_count default to the widest RentCast allows
        (compCount maxes at 25) so the results page's nearby-sales radius
        filter (up to NEARBY_SALES_MAX_RADIUS_MILES) has enough comps to
        filter client-side without a second call. This also widens the
        input to RentCast's own AVM price calculation compared to its
        undocumented default — consistent with the existing thin-comps/
        rural-area confidence risk already called out in the plan, not a
        new category of risk.
        """
        return self._get('/avm/value', {'address': address, 'maxRadius': max_radius, 'compCount': comp_count})

    def get_property_record(self, address):
        """
        Property Records endpoint. Returns an array (an address can match
        multiple parcels); we want the first/best match. Adds zoning and
        subdivision, which the Value Estimate response doesn't include.
        https://developers.rentcast.io/reference/property-records
        """
        records = self._get('/properties', {'address': address})
        if not records:
            raise RentCastNotFoundError(f'No property record found for address={address!r}')
        return records[0]

    def lookup_property(self, address, force_refresh=False):
        """
        Address in, full property snapshot out — from cache if we've seen
        this address before, otherwise via 2 live RentCast calls (which are
        then cached indefinitely, and used to free-seed cache rows for any
        comps returned alongside it — see _seed_comps_cache, 0 extra calls).

        A cache hit whose source is 'comp_seed' (pre-seeded for free from
        another address's comps, not looked up for this address directly)
        is missing zoning/subdivision/history and has no real AVM price —
        see models.PropertyLookupCache. Pass force_refresh=True to force 2
        real calls, upgrading a comp_seed row to a full 'full_lookup' one.

        force_refresh only takes effect against a 'comp_seed' row — an
        existing 'full_lookup' row is always served from cache regardless
        of the flag. Callers (e.g. the results page's "refresh with full
        data" button) only make sense against comp_seed data; honoring the
        flag unconditionally would let any POST to /analyses force 2 paid
        RentCast calls on an address that already has a real cached
        lookup, defeating the whole point of caching.

        Returns (avm_json, property_json, from_cache, source).
        """
        normalized = normalize_address(address)
        cached = PropertyLookupCache.query.filter_by(normalized_address=normalized).first()
        should_refresh = force_refresh and cached is not None and cached.source == 'comp_seed'
        if cached is not None and not should_refresh:
            logger.info('RentCast cache hit for %r (%s) — 0 API calls spent', normalized, cached.source)
            return cached.raw_avm_json, cached.raw_property_json, True, cached.source

        avm_json = self.get_value_estimate(address)
        property_json = self.get_property_record(address)

        if cached is not None:
            # force_refresh upgrading an existing row (typically comp_seed)
            # to a real one, in place — normalized_address is unique, so
            # this must be an UPDATE, not a second INSERT.
            cached.source = 'full_lookup'
            cached.raw_avm_json = avm_json
            cached.raw_property_json = property_json
        else:
            db.session.add(PropertyLookupCache(
                normalized_address=normalized,
                source='full_lookup',
                raw_avm_json=avm_json,
                raw_property_json=property_json,
            ))
        try:
            db.session.commit()
        except IntegrityError:
            # Another concurrent request for the same new address (double
            # form submit, two tabs, etc.) won the race and already wrote
            # this normalized_address — normal, not a real failure. Both
            # requests already spent their 2 live calls by this point, but
            # rolling back and reusing the row that won avoids compounding
            # that with a 500 on top of it.
            db.session.rollback()
            logger.warning(
                'RentCast cache write race for %r — another request already cached it', normalized
            )
            cached = PropertyLookupCache.query.filter_by(normalized_address=normalized).first()
            return cached.raw_avm_json, cached.raw_property_json, True, cached.source

        logger.info('RentCast cache miss for %r — 2 API calls spent, now cached', normalized)
        self._seed_comps_cache(avm_json.get('comparables') or [])

        return avm_json, property_json, False, 'full_lookup'

    def _seed_comps_cache(self, comparables):
        """
        Pre-seed property_lookup_cache from a Value Estimate response's
        comparables[] — for free, since this data is already part of the
        response just paid for. Skips any address already cached under
        any source, so a real 'full_lookup' row is never clobbered by
        partial comp data, and nothing gets seeded twice.

        The resulting rows are deliberately partial: no zoning/subdivision
        /history (Property Records-only fields), no comps of their own,
        and their "market value" is just the comp's own sale/listing
        price, not an independent AVM computation — see
        models.PropertyLookupCache and services/analyzer.py's handling of
        source == 'comp_seed'.
        """
        for comp in comparables:
            address = comp.get('formattedAddress')
            if not address:
                continue
            normalized = normalize_address(address)
            if PropertyLookupCache.query.filter_by(normalized_address=normalized).first() is not None:
                continue

            db.session.add(PropertyLookupCache(
                normalized_address=normalized,
                source='comp_seed',
                raw_avm_json={
                    'price': comp.get('price'),
                    'priceRangeLow': None,
                    'priceRangeHigh': None,
                    'subjectProperty': {
                        'squareFootage': comp.get('squareFootage'),
                        'lotSize': comp.get('lotSize'),
                        'bedrooms': comp.get('bedrooms'),
                        'bathrooms': comp.get('bathrooms'),
                        'yearBuilt': comp.get('yearBuilt'),
                        'latitude': comp.get('latitude'),
                        'longitude': comp.get('longitude'),
                    },
                    'comparables': [],
                },
                raw_property_json={'zoning': None, 'subdivision': None, 'history': None},
            ))

        try:
            db.session.commit()
        except IntegrityError:
            # A concurrent request seeded (or fully looked up) one of
            # these same addresses first — fine, just drop this attempt
            # rather than fail the whole lookup over a free side effect.
            db.session.rollback()
            logger.warning('Comp cache seeding race — another request seeded an overlapping address')
