"""
Address suggestions for the analyze form, so a property can be picked
from a list instead of typed out in full.

Suggestions come from Photon (photon.komoot.io), a free type-ahead
geocoder built on OpenStreetMap data. No API key, no bill, and no
signup. In exchange its house-number coverage varies by area: when it
has nothing for what's typed, the form behaves exactly as it always has
and the address is typed in full.

What comes back is filtered hard, because the address chosen here is
what RentCast, the Census geocoder and every zoning lookup downstream
are given:
  * US only. Photon answers worldwide and happily offers a Lexington in
    England for a Massachusetts query.
  * Real street addresses only, with a house number and a street. Photon
    also returns bus stops, parks and shops, none of which can be
    analyzed as a property.
  * States abbreviated to "MA", the shape the rest of the app parses a
    jurisdiction out of (see services/build_limits_search.py and
    services/zoning_chat.py in their branches, plus the Census geocoder).
  * No postal code. OSM's are often absent or belong to a neighbouring
    town, and a wrong one is worse than none for matching a property.
  * Loose matches dropped. Photon always answers with its nearest
    guesses, so "5625 Forbes Ave" offers 3700 Forbes Avenue and a
    typo offers three unrelated streets. A suggestion that silently
    swaps the house number is the one mistake here that would analyze
    the wrong property, so a typed house number has to match and a
    typed word has to appear.

Photon asks callers to be gentle with the public instance, so results
are cached here for a few minutes and the form only asks after a pause
in typing.

Never raises: no answer is an empty list, and the form still works.
"""

import logging
import re
import time

import requests

logger = logging.getLogger(__name__)

PHOTON_URL = 'https://photon.komoot.io/api/'
USER_AGENT = 'value-predictor/1.0 (address autocomplete; see integrations/address_suggest.py)'
REQUEST_TIMEOUT = 4  # seconds; a suggestion that arrives late is worthless
MIN_QUERY_CHARS = 3
MAX_SUGGESTIONS = 5
CACHE_SECONDS = 300
CACHE_ENTRIES = 200

# Photon returns the state spelled out; everything downstream parses
# "City, ST".
STATE_ABBREVIATIONS = {
    'alabama': 'AL', 'alaska': 'AK', 'arizona': 'AZ', 'arkansas': 'AR', 'california': 'CA',
    'colorado': 'CO', 'connecticut': 'CT', 'delaware': 'DE', 'district of columbia': 'DC',
    'florida': 'FL', 'georgia': 'GA', 'hawaii': 'HI', 'idaho': 'ID', 'illinois': 'IL',
    'indiana': 'IN', 'iowa': 'IA', 'kansas': 'KS', 'kentucky': 'KY', 'louisiana': 'LA',
    'maine': 'ME', 'maryland': 'MD', 'massachusetts': 'MA', 'michigan': 'MI', 'minnesota': 'MN',
    'mississippi': 'MS', 'missouri': 'MO', 'montana': 'MT', 'nebraska': 'NE', 'nevada': 'NV',
    'new hampshire': 'NH', 'new jersey': 'NJ', 'new mexico': 'NM', 'new york': 'NY',
    'north carolina': 'NC', 'north dakota': 'ND', 'ohio': 'OH', 'oklahoma': 'OK', 'oregon': 'OR',
    'pennsylvania': 'PA', 'rhode island': 'RI', 'south carolina': 'SC', 'south dakota': 'SD',
    'tennessee': 'TN', 'texas': 'TX', 'utah': 'UT', 'vermont': 'VT', 'virginia': 'VA',
    'washington': 'WA', 'west virginia': 'WV', 'wisconsin': 'WI', 'wyoming': 'WY',
    'puerto rico': 'PR', 'guam': 'GU', 'u.s. virgin islands': 'VI', 'american samoa': 'AS',
    'northern mariana islands': 'MP',
}

_cache = {}  # query -> (expires_at, [addresses])


def suggest_addresses(query, session=None, limit=MAX_SUGGESTIONS):
    """
    Up to `limit` US street addresses matching what's been typed, as
    plain strings like '28 Lillian Road, Lexington, MA'. Empty for a
    short query, no matches, or any failure.
    """
    cleaned = ' '.join((query or '').split())
    if len(cleaned) < MIN_QUERY_CHARS:
        return []

    cached = _cached(cleaned)
    if cached is not None:
        return cached[:limit]

    session = session or _default_session()
    try:
        response = session.get(PHOTON_URL, params={'q': cleaned, 'limit': 12, 'lang': 'en'},
                               timeout=REQUEST_TIMEOUT, headers={'Accept': 'application/json'})
        response.raise_for_status()
        payload = response.json()
    except (requests.RequestException, ValueError) as exc:
        logger.info('Address suggestions unavailable for %r: %s', cleaned, exc)
        return []

    addresses = []
    for feature in (payload.get('features') or []) if isinstance(payload, dict) else []:
        properties = feature.get('properties') if isinstance(feature, dict) else None
        address = _format(properties)
        if address and address not in addresses and _matches(cleaned, properties, address):
            addresses.append(address)
    _store(cleaned, addresses)
    return addresses[:limit]


def _format(properties):
    """'28 Lillian Road, Lexington, MA', or '' if this isn't a US street address."""
    if not isinstance(properties, dict):
        return ''
    if (properties.get('countrycode') or '').upper() != 'US':
        return ''
    housenumber = _text(properties.get('housenumber'))
    street = _text(properties.get('street'))
    # A house number and street is what makes this a property rather than
    # a park, a bus stop or a town centre.
    if not (housenumber and street):
        return ''
    city = _text(properties.get('city')) or _text(properties.get('county'))
    state = STATE_ABBREVIATIONS.get(_text(properties.get('state')).lower(), '')
    if not (city and state):
        return ''
    return f'{housenumber} {street}, {city}, {state}'


def _matches(query, properties, address):
    """
    Whether a suggestion is plausibly what was typed, rather than
    Photon's nearest guess at it.
    """
    tokens = re.findall(r'[a-z0-9]+', query.lower())
    if not tokens:
        return False
    if tokens[0].isdigit() and tokens[0] != _text(properties.get('housenumber')):
        return False
    words = [token for token in tokens if not token.isdigit() and len(token) > 2]
    if not words:
        return True  # nothing but a house number typed so far
    haystack = normalized(address)
    return any(word in haystack for word in words)


def _text(value):
    return ' '.join(str(value).split()) if isinstance(value, str) else ''


def normalized(address):
    """A comparable form, for telling two spellings of one address apart."""
    return re.sub(r'[^a-z0-9]+', ' ', (address or '').lower()).strip()


def _cached(query):
    entry = _cache.get(query)
    if entry is None:
        return None
    expires_at, addresses = entry
    if expires_at < time.monotonic():
        _cache.pop(query, None)
        return None
    return addresses


def _store(query, addresses):
    if len(_cache) >= CACHE_ENTRIES:
        # Cheapest possible eviction: the whole thing. It only ever holds
        # a few minutes of typing.
        _cache.clear()
    _cache[query] = (time.monotonic() + CACHE_SECONDS, addresses)


def _default_session():
    session = requests.Session()
    session.headers['User-Agent'] = USER_AGENT
    return session
