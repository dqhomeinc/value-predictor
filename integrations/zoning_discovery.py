"""
Nationwide zoning discovery: find and query a jurisdiction's own zoning
GIS for any US address, without a hand-written adapter per city.

Two free, keyless, nationwide primitives make this possible:
  1. The Census Bureau geocoder resolves any US address to coordinates
     plus its incorporated place, county and state.
  2. ArcGIS Online's federated search indexes the public GIS services
     that most US municipalities publish to, including zoning.

So the pipeline is: geocode -> search for that jurisdiction's zoning
service -> pick the right layer -> point-in-polygon query -> read the
zoning code off the matched feature.

This is emphatically not "scrape the web". It's a documented search API
over machine-readable services, which is why it can generalize where HTML
scraping can't.

WHAT IT GETS WRONG, AND THE GUARDS FOR EACH
Measured against real cities while building this, naive discovery fails in
three ways that all produce *confidently wrong* answers — worse than no
answer for a tool people make financial decisions from:

  * Wrong layer. Austin's top hit was its zoning *ordinance* layer, whose
    "code" is an ordinance number like '19990225-070b'. Guard: score
    layers as well as services (_score_layer), and validate that the value
    actually looks like a zoning code (_looks_like_zoning_code).
  * Wrong jurisdiction. An address in Montpelier, VT matched a service for
    Middlesex County, NJ. Guard: the state must match (_score_item returns
    None to disqualify outright, never merely down-rank).
  * Untrustworthy source. Nashville's best hit was published by a private
    engineering consultant, NYC's by a personal account. Data may be fine
    or years stale, and there's no way to tell from here. Guard: don't
    reject it, but classify provenance (OFFICIAL vs UNVERIFIED) so the UI
    can say where a number came from rather than implying the city
    published it.

Coverage is real but partial, and always will be: some jurisdictions
publish nothing machine-readable at all. "Not found" is a normal outcome.
"""

import logging
import re
from dataclasses import dataclass, field

import requests

logger = logging.getLogger(__name__)

DEFAULT_TIMEOUT = 15
USER_AGENT = 'value-predictor/1.0 (zoning discovery; see integrations/zoning_discovery.py)'

CENSUS_GEOCODE_URL = 'https://geocoding.geo.census.gov/geocoder/geographies/onelineaddress'
AGOL_SEARCH_URL = 'https://www.arcgis.com/sharing/rest/search'

# Provenance of a discovered service.
OFFICIAL = 'official'      # published by an account that looks like the jurisdiction itself
UNVERIFIED = 'unverified'  # a real zoning layer, but from a third-party account

# Trailing words in Census place names that aren't part of the city's name
# ('Pittsburgh city' -> 'Pittsburgh').
_PLACE_SUFFIX = re.compile(r'\s+(city|town|borough|village|township|municipality|CDP)$', re.IGNORECASE)

# Layer/service names that carry something other than base zoning. Matching
# one disqualifies the layer — these are the false positives that produced
# ordinance numbers and overlay names instead of zoning codes.
_LAYER_REJECT = (
    'ordinance', 'case', 'petition', 'appeal', 'amendment', 'proposed', 'future',
    'historic', 'flood', 'buffer', 'parking', 'police', 'overlay', 'label',
    'annex', 'subdivision', 'parcel', 'address', 'boundary', 'council',
)
_LAYER_PREFER = ('zoning', 'zone district', 'zonedist', 'base zoning', 'zoning district')

# Field names that plausibly hold a zoning code.
_ZONE_FIELD = re.compile(r'zon(e|ing)?', re.IGNORECASE)
# Field names that look like a code rather than prose or a document pointer.
_FIELD_REJECT = re.compile(r'(ordinance|url|path|link|hyperlink|date|id$|_id$|objectid|shape)', re.IGNORECASE)

# A zoning code is short and code-like: 'RM-M', 'SF-3-HD-NCCD-NP', 'C5-3',
# 'D-C'. Not a URL, a date, a sentence, or a bare number.
_CODE_OK = re.compile(r'^[A-Za-z][A-Za-z0-9 ./&\-]{0,29}$')
_LOOKS_LIKE_DATE = re.compile(r'^\d{6,8}')


class ZoningDiscoveryError(Exception):
    """No usable zoning source was found for this address."""


@dataclass
class Jurisdiction:
    place: str
    county: str
    state: str
    lon: float
    lat: float

    @property
    def city(self):
        return _PLACE_SUFFIX.sub('', self.place).strip()

    @property
    def key(self):
        """Stable cache key — discovery is per jurisdiction, not per address."""
        return f'{self.city}|{self.county}|{self.state}'.upper()

    @property
    def label(self):
        return f'{self.city}, {self.state}'


@dataclass
class DiscoveredZoning:
    zoning_code: str
    zoning_description: str = ''
    jurisdiction: str = ''
    provenance: str = UNVERIFIED
    service_title: str = ''
    service_owner: str = ''
    service_url: str = ''
    layer_id: int = 0
    reference_url: str = ''  # a code/ordinance link carried on the feature, when present
    extras: dict = field(default_factory=dict)


def discover_zoning(address, session=None):
    """
    Address in, DiscoveredZoning out. Raises ZoningDiscoveryError whenever
    no sufficiently trustworthy answer is found — callers should treat that
    as "no data", which is a normal outcome for much of the country.
    """
    session = session or _default_session()
    jurisdiction = geocode(address, session)
    return discover_for_jurisdiction(jurisdiction, session)


def discover_for_jurisdiction(jurisdiction, session=None):
    session = session or _default_session()
    candidates = search_services(jurisdiction, session)
    if not candidates:
        raise ZoningDiscoveryError(f'No zoning services found for {jurisdiction.label}')

    for item, provenance in candidates:
        found = _try_service(item, provenance, jurisdiction, session)
        if found is not None:
            return found

    raise ZoningDiscoveryError(f'No usable zoning layer for {jurisdiction.label}')


def geocode(address, session=None):
    """Census Bureau geocoder — free, keyless, nationwide, authoritative."""
    session = session or _default_session()
    try:
        payload = _get(session, CENSUS_GEOCODE_URL, {
            'address': address,
            'benchmark': 'Public_AR_Current',
            'vintage': 'Current_Current',
            'format': 'json',
        })
    except requests.RequestException as exc:
        raise ZoningDiscoveryError(f'Census geocoder failed for {address!r}: {exc}') from exc

    matches = (payload.get('result') or {}).get('addressMatches') or []
    if not matches:
        raise ZoningDiscoveryError(f'Census geocoder found no match for {address!r}')

    best = matches[0]
    geo = best.get('geographies') or {}
    place = ((geo.get('Incorporated Places') or geo.get('County Subdivisions') or [{}])[0]).get('NAME', '')
    county = ((geo.get('Counties') or [{}])[0]).get('NAME', '')
    state = ((geo.get('States') or [{}])[0]).get('STUSAB', '')
    coords = best.get('coordinates') or {}

    if not state or coords.get('x') is None:
        raise ZoningDiscoveryError(f'Census geocoder returned an unusable match for {address!r}')

    return Jurisdiction(place=place, county=county, state=state, lon=coords['x'], lat=coords['y'])


def search_services(jurisdiction, session=None):
    """
    Candidate zoning services for a jurisdiction, best first, each paired
    with its provenance. Anything failing the state check is dropped
    entirely rather than ranked low — a neighbouring state's zoning is not
    a worse answer, it's a wrong one.
    """
    session = session or _default_session()
    city, state = jurisdiction.city, jurisdiction.state
    queries = [
        f'zoning {city} {state} type:"Feature Service"',
        f'zoning "{city}" type:"Feature Service"',
        f'zoning {jurisdiction.county} {state} type:"Feature Service"',
    ]

    seen = {}
    for query in queries:
        try:
            payload = _get(session, AGOL_SEARCH_URL, {'q': query, 'f': 'json', 'num': 20})
        except requests.RequestException as exc:
            logger.warning('AGOL search failed for %r: %s', query, exc)
            continue
        for item in payload.get('results') or []:
            if item.get('url'):
                seen.setdefault(item['url'], item)

    scored = []
    for item in seen.values():
        result = _score_item(item, jurisdiction)
        if result is None:
            continue
        score, provenance = result
        scored.append((score, provenance, item))

    scored.sort(key=lambda triple: triple[0], reverse=True)
    return [(item, provenance) for _, provenance, item in scored]


def _score_item(item, jurisdiction):
    """
    (score, provenance), or None to disqualify. Disqualification is
    reserved for answers that would be wrong rather than merely weak: a
    different state, or a service with no connection to this jurisdiction.
    """
    title = (item.get('title') or '').lower()
    owner = (item.get('owner') or '').lower()
    snippet = (item.get('snippet') or '').lower()
    haystack = f'{title} {snippet}'
    city = jurisdiction.city.lower()
    state = jurisdiction.state.lower()
    county = jurisdiction.county.lower().replace(' county', '')

    # Hard state check. A title naming a different state is the Montpelier
    # VT -> Middlesex County NJ failure, and it looks perfectly plausible
    # otherwise, so it has to be excluded rather than down-ranked.
    if _names_a_different_state(title, state):
        return None

    score = 0
    if 'zoning' in title or 'zone' in title:
        score += 5
    city_hit = bool(city) and (city in haystack or city.replace(' ', '') in owner.replace('.', '').replace('_', ''))
    county_hit = bool(county) and county in haystack
    if not (city_hit or county_hit):
        return None
    if city_hit:
        score += 5
    if county_hit:
        score += 2
    if re.search(rf'\b{state}\b', haystack):
        score += 2

    # Provenance: does the publishing account look like the jurisdiction?
    condensed_owner = owner.replace('.', '').replace('_', '').replace('-', '')
    condensed_city = city.replace(' ', '')
    official = bool(condensed_city) and (
        condensed_city in condensed_owner
        or condensed_city[:4] in condensed_owner and len(condensed_city) >= 4
    )
    if official:
        score += 6

    for bad in _LAYER_REJECT:
        if bad in title:
            score -= 4

    return score, (OFFICIAL if official else UNVERIFIED)


def _names_a_different_state(title, state):
    """
    Whether a title identifies a state other than this one.

    Two constraints keep this from throwing away valid coverage, because
    a false positive here silently costs real coverage — the same
    "quietly wrong" failure as returning a wrong answer, pointed the
    other way:

    * Title only. Snippets are free prose, and half the state
      abbreviations are ordinary English words (in, or, hi, me, ok, la).
      A Pittsburgh service described as "Zoning districts in the City of
      Pittsburgh" contains "in", which as a bare word match reads as
      Indiana and drops a correct Pennsylvania result.
    * The abbreviation has to sit where a state actually sits — after a
      comma, or at the end of the title ("Middlesex County, NJ"). That's
      how these titles encode a state, and it's what the original
      wrong-state match looked like.

    Full state names are deliberately not matched: too many double as
    place names ("Washington County, VT" would read as Washington state),
    which would reintroduce exactly the false positives this avoids.
    """
    for other in _STATE_ABBR:
        if other == state:
            continue
        if re.search(rf',\s*{other}\b', title) or re.search(rf'\b{other}$', title.strip()):
            return True
    return False


def _try_service(item, provenance, jurisdiction, session):
    url = item['url']
    try:
        meta = _get(session, url, {'f': 'json'})
    except requests.RequestException as exc:
        logger.info('Zoning service metadata failed for %s: %s', url, exc)
        return None

    layers = [
        layer for layer in (meta.get('layers') or [])
        if layer.get('geometryType') in (None, 'esriGeometryPolygon')
    ]
    if not layers:
        layers = [{'id': 0, 'name': item.get('title', '')}]

    ranked = sorted(layers, key=lambda layer: _score_layer(layer), reverse=True)
    for layer in ranked[:4]:
        # Must affirmatively look like a zoning layer, not merely fail to
        # look like something else. Austin Public Health's service and a
        # power authority's grid both cleared a "not rejected" bar and
        # returned junk that passed for a zoning code.
        if _score_layer(layer) <= 0:
            continue
        attrs = _query_point(session, url, layer.get('id', 0), jurisdiction)
        if not attrs:
            continue
        code, description = _pick_zoning_fields(attrs)
        if not code:
            continue
        return DiscoveredZoning(
            zoning_code=code,
            zoning_description=description,
            jurisdiction=jurisdiction.label,
            provenance=provenance,
            service_title=item.get('title', ''),
            service_owner=item.get('owner', ''),
            service_url=url,
            layer_id=layer.get('id', 0),
            reference_url=_pick_reference_url(attrs),
        )
    return None


def _score_layer(layer):
    name = (layer.get('name') or '').lower()
    score = 0
    for good in _LAYER_PREFER:
        if good in name:
            score += 4
            break
    for bad in _LAYER_REJECT:
        if bad in name:
            score -= 6
    return score


def _query_point(session, service_url, layer_id, jurisdiction):
    try:
        payload = _get(session, f'{service_url}/{layer_id}/query', {
            'geometry': f'{jurisdiction.lon},{jurisdiction.lat}',
            'geometryType': 'esriGeometryPoint',
            'inSR': 4326,
            'spatialRel': 'esriSpatialRelIntersects',
            'outFields': '*',
            'returnGeometry': 'false',
            'f': 'json',
        })
    except requests.RequestException as exc:
        logger.info('Zoning point query failed for %s/%s: %s', service_url, layer_id, exc)
        return None

    if 'error' in payload:
        return None
    features = payload.get('features') or []
    return features[0].get('attributes') if features else None


def _pick_zoning_fields(attrs):
    """
    (code, description) from a matched feature's attributes.

    The code has to survive _looks_like_zoning_code — without that check,
    Austin's ordinance layer yields '19990225-070b' and it reads as a
    perfectly good zoning code to anything that only checks the field name.
    """
    candidates = []
    for key, value in (attrs or {}).items():
        if not isinstance(value, str) or not value.strip():
            continue
        if not _ZONE_FIELD.search(key) or _FIELD_REJECT.search(key):
            continue
        candidates.append((key, value.strip()))

    codes = [(k, v) for k, v in candidates if _looks_like_zoning_code(v)]
    if not codes:
        return '', ''

    codes.sort(key=lambda kv: len(kv[1]))
    code = codes[0][1]

    # A longer sibling value is the human-readable district name.
    description = ''
    for _, value in sorted(candidates, key=lambda kv: len(kv[1]), reverse=True):
        if value != code and len(value) > len(code) + 3:
            description = value
            break
    return code, description


def _looks_like_zoning_code(value):
    """
    Zoning codes look like 'RM-M', 'SF-3', 'C5-3', 'D-C', or a spelled-out
    district name like 'Village District'. They do not look like 'y' or
    'J' — single letters were what unrelated layers (a Spanish-language
    county boundary, a power authority's grid) yielded when only the field
    *name* was checked, and they read as plausible codes to a human
    skimming the page. Requiring real structure is what keeps a wrong
    answer from being indistinguishable from a right one.
    """
    if not _CODE_OK.match(value) or _LOOKS_LIKE_DATE.match(value):
        return False
    if value.lower().startswith(('http', 'www.')):
        return False
    if len(value) < 2:
        return False

    has_digit = any(char.isdigit() for char in value)
    has_separator = '-' in value or '.' in value
    # Multi-word names ('Village District', 'Rural Residential') are real
    # in small towns; single bare words ('Yes', 'Downtown') are usually a
    # flag or label column, not a district code.
    is_multiword = len(value.split()) >= 2
    is_code_shaped = len(value) <= 12 and value.replace(' ', '').isupper()

    return has_digit or has_separator or is_multiword or is_code_shaped


def _pick_reference_url(attrs):
    for key, value in (attrs or {}).items():
        if (isinstance(value, str) and value.lower().startswith('http')
                and re.search(r'(code|ordinance|municode|zoning|ecode)', f'{key} {value}', re.IGNORECASE)):
            return value.strip()
    return ''


_STATE_ABBR = {
    'al', 'ak', 'az', 'ar', 'ca', 'co', 'ct', 'de', 'fl', 'ga', 'hi', 'id', 'il', 'in', 'ia',
    'ks', 'ky', 'la', 'me', 'md', 'ma', 'mi', 'mn', 'ms', 'mo', 'mt', 'ne', 'nv', 'nh', 'nj',
    'nm', 'ny', 'nc', 'nd', 'oh', 'ok', 'or', 'pa', 'ri', 'sc', 'sd', 'tn', 'tx', 'ut', 'vt',
    'va', 'wa', 'wv', 'wi', 'wy', 'dc',
}


def _default_session():
    session = requests.Session()
    session.headers['User-Agent'] = USER_AGENT
    return session


def _get(session, url, params):
    response = session.get(url, params=params, timeout=DEFAULT_TIMEOUT, headers={'Accept': 'application/json'})
    response.raise_for_status()
    return response.json()
