"""
Property-specific zoning and build-restriction lookup, from a
jurisdiction's own public GIS services.

Two distinct things this fills in that RentCast can't:
  1. Zoning at all — RentCast's Property Records often has no zoning field.
  2. The parcel-specific restrictions that actually govern a teardown and
     rebuild. RentCast at best gives a bare base code ("R-1"); it never
     tells you the parcel sits in a local historic district (where
     demolition may be prohibited outright), inside a Neighborhood
     Conservation Combining District, under Austin's Subchapter F
     residential design standards, in a floodplain, or under an overlay
     with its own height cap. Those are per-parcel, they change what you
     can build, and they're exactly what a demolish-and-rebuild decision
     turns on.

Both come from the same place: point-in-polygon `identify` queries against
the jurisdiction's ArcGIS REST MapServers — stable, documented JSON APIs
the city runs for machine consumption. Deliberately *not* scraping
rendered municipal web pages or code-hosting sites: those have no schema
contract, actively block automated access, and would fail silently with
wrong numbers rather than loudly with no data. For a tool people make
financial decisions from, "no data" beats "quietly wrong data".

Not a general-purpose "any US address" solution — no such thing exists,
free or paid (even nationwide commercial zoning APIs cap out at major
metros). Every jurisdiction publishes differently, so this is an explicit
per-jurisdiction registry, not a crawler. Today: Austin, TX, verified
end-to-end against the live services below. To add a jurisdiction, write a
`_lookup_<city>_zoning(address, session)` returning a MunicipalZoningResult
and register it in _detect_jurisdiction.
"""

import logging
import re
from dataclasses import dataclass, field

import requests

logger = logging.getLogger(__name__)

DEFAULT_TIMEOUT = 10
USER_AGENT = 'value-predictor/1.0 (municipal zoning lookup; see integrations/municipal_zoning.py)'

AUSTIN_BASE = 'https://maps.austintexas.gov/arcgis/rest/services'
AUSTIN_GEOCODE_URL = f'{AUSTIN_BASE}/Geocode/COA_Locator/GeocodeServer/findAddressCandidates'
# ArcGIS geocoder match confidence, 0-100. Below this, treat it as "didn't
# find this address" rather than risk reporting another parcel's rules.
AUSTIN_MIN_GEOCODE_SCORE = 80

# MapServers worth an identify call, in priority order. Each is one HTTP
# call, so this list is kept to services that carry genuine build
# restrictions — Zoning_4 (infill *options*) is deliberately omitted, as
# those are permissive development bonuses, not constraints.
AUSTIN_IDENTIFY_SERVICES = (
    'Shared/Zoning_1',    # base zoning, governing ordinances, case manager
    'Shared/Zoning_2',    # overlays: NCCD, ETOD, design standards, corridors
    'Shared/Zoning_3',    # historic landmarks and districts
    'Shared/Floodplain',  # absence of results here is itself the useful signal
    'Shared/Property',    # jurisdiction (full purpose vs limited vs ETJ)
)

# How a matched layer maps onto a teardown-and-rebuild decision.
#   'critical' — may block demolition or rebuild outright
#   'high'     — materially constrains what can be built
#   'info'     — context, no direct constraint
_CRITICAL_LAYERS = {
    'City of Austin Historic Landmarks',
    'Local Historic Districts',
    'National Register of Historic Districts',
}
_HIGH_LAYERS = {
    'Neighborhood Conservation Combining District',
    'Residential Design Standards',
    'Capitol View Corridors',
    'Capitol Dominance Overlay',
    'Hill Country Roadways Overlay',
    'Lake Austin Overlay',
    'Waterfront Overlay',
    'Waterfront Setbacks Overlay',
    'Wildland Urban Interface 2024',
    'ETOD Overlay',
    'Urban Renewal Overlay',
    'West Campus Neighborhood Overlay',
    'Barton Springs Overlay',
    'Airport Overlay',
    'Hazardous Pipelines',
}
# Layers that say nothing about buildability — skipped entirely so the
# results page shows constraints, not noise.
_IGNORED_LAYERS = {
    'Zoning', 'Zoning Text', 'Zoning Ordinance', 'Zoning Case Managers',
    'Addresses', 'Counties', 'Impervious Cover', 'Selected Sign Ordinances',
    'Selected Sound Ordinances', 'Jurisdictions (No Fill)', 'Council Districts',
    'Streets', 'Street Labels', 'Parcels', 'Lot Lines',
}


class MunicipalZoningUnavailableError(Exception):
    """
    No municipal zoning data for this address — an unsupported
    jurisdiction, a supported one where geocoding came back with no
    confident match, or a request to the GIS server failed. Always the
    expected "no answer" outcome for callers to degrade gracefully on,
    never a bug to fix on this end.
    """


@dataclass
class ZoningRestriction:
    label: str          # the layer's own name, e.g. 'Local Historic Districts'
    detail: str = ''    # e.g. 'HYDE PARK', or an ordinance number
    severity: str = 'info'  # 'critical' | 'high' | 'info'
    url: str = ''


# Shape version of MunicipalZoningResult.as_dict(), stored with every cached
# result. Bump it when the stored detail gains information worth re-fetching
# for: services/analyzer.py looks up again for any cached detail from an
# older version rather than serving it forever. 1 is the first versioned
# shape; detail stored without a version predates it. 2 added the matched
# layer and its last-updated date.
DETAIL_VERSION = 2


@dataclass
class MunicipalZoningResult:
    zoning_code: str
    source: str  # e.g. 'austin_gis', 'discovered'
    restrictions: list = field(default_factory=list)  # of ZoningRestriction
    jurisdiction: str = ''
    in_floodplain: bool = False
    ordinances: list = field(default_factory=list)  # of {'number', 'url'}
    case_manager: dict = field(default_factory=dict)  # {'name', 'phone'}
    # Set by discovery (integrations/zoning_discovery.py): the district's
    # human-readable name, and where the answer came from: the layer, its
    # publisher, and when its data was last edited. The page reports those
    # facts rather than any claim about who's official. `provenance` is
    # kept only as the ranking hint discovery used.
    zoning_description: str = ''
    provenance: str = ''  # '' | 'official' | 'unverified'; ranking hint, never shown
    service_title: str = ''
    service_owner: str = ''
    reference_url: str = ''
    layer_name: str = ''
    data_updated: str = ''  # ISO date, when the layer publishes one

    def as_dict(self):
        """Plain JSON-safe dict, for the JSON columns on
        PropertyLookupCache/Analysis and the results template."""
        return {
            'zoning_code': self.zoning_code,
            'source': self.source,
            'jurisdiction': self.jurisdiction,
            'in_floodplain': self.in_floodplain,
            'ordinances': self.ordinances,
            'case_manager': self.case_manager,
            'zoning_description': self.zoning_description,
            'provenance': self.provenance,
            'service_title': self.service_title,
            'service_owner': self.service_owner,
            'reference_url': self.reference_url,
            'layer_name': self.layer_name,
            'data_updated': self.data_updated,
            'detail_version': DETAIL_VERSION,
            'restrictions': [
                {'label': r.label, 'detail': r.detail, 'severity': r.severity, 'url': r.url}
                for r in self.restrictions
            ],
        }


def lookup_municipal_zoning(address, session=None):
    """
    Address in, MunicipalZoningResult out.

    Curated adapters first, nationwide discovery second. A curated adapter
    is worth preferring where one exists: it knows which of a city's many
    layers carry overlays, historic designations and floodplain, so it
    returns the restrictions that decide a teardown. Discovery
    (integrations/zoning_discovery.py) generalizes to the rest of the
    country but only reliably recovers the base zoning district.

    Raises MunicipalZoningUnavailableError for every "no answer" case —
    callers should catch that one exception and carry on.
    """
    session = session or requests.Session()

    lookup = _detect_jurisdiction(address)
    if lookup is not None:
        return lookup(address, session)

    return _lookup_via_discovery(address, session)


def _lookup_via_discovery(address, session):
    # Imported here rather than at module scope so the curated path doesn't
    # depend on the discovery machinery (and tests of one don't drag in the
    # other).
    from integrations.zoning_discovery import ZoningDiscoveryError, discover_zoning

    try:
        found = discover_zoning(address, session=session)
    except ZoningDiscoveryError as exc:
        raise MunicipalZoningUnavailableError(str(exc)) from exc

    return MunicipalZoningResult(
        zoning_code=found.zoning_code,
        source='discovered',
        jurisdiction=found.jurisdiction,
        zoning_description=found.zoning_description,
        provenance=found.provenance,
        service_title=found.service_title,
        service_owner=found.service_owner,
        reference_url=found.reference_url,
        layer_name=found.layer_name,
        data_updated=found.data_updated,
    )


# Human-readable coverage, for telling a user why an address returned
# nothing. Keep in step with _detect_jurisdiction below.
SUPPORTED_JURISDICTIONS = ('Austin, TX',)


def has_jurisdiction_adapter(address):
    """
    Whether any jurisdiction adapter claims this address — i.e. whether a
    lookup could return anything at all. Lets callers (notably the local
    dev mock) mirror real coverage without reaching into the registry.
    """
    return _detect_jurisdiction(address) is not None


# "Austin" only counts as the city when the state follows it directly.
# Matching it anywhere before a "TX" also catches addresses where Austin
# is a street or business name in a different Texas city — "123 Austin
# Ave, Georgetown, TX", "500 W Austin St, Marble Falls, TX". Those would
# be handed to Austin's own geocoder, which fuzzy-matches a single line
# and can return a confident match on a similarly named Austin street:
# a wrong parcel's rules, presented as this parcel's. Requiring the state
# to follow immediately is what separates the city from a street name,
# while still accepting the comma-less addresses people actually type.
# The trailing lookahead keeps the state from being a street name too
# ("Austin Texas Ave"), by requiring a ZIP, a comma, or end of string.
_AUSTIN_ADDRESS = re.compile(
    r'\bAustin\b,?\s*(?:TX|Texas)\b(?=\s*(?:\d{5}|,|$))',
    re.IGNORECASE,
)


def _detect_jurisdiction(address):
    if _AUSTIN_ADDRESS.search(address):
        return _lookup_austin_zoning
    return None


def _lookup_austin_zoning(address, session):
    location = _geocode_austin(address, session)

    result = MunicipalZoningResult(zoning_code='', source='austin_gis')
    seen_layers = set()

    for service in AUSTIN_IDENTIFY_SERVICES:
        try:
            matches = _identify(session, service, location)
        except requests.RequestException as exc:
            # One flaky service shouldn't lose the rest of the picture —
            # note it and keep going, rather than failing the lookup.
            logger.warning('Austin identify failed for %s at %r: %s', service, address, exc)
            continue

        if service == 'Shared/Floodplain' and matches:
            result.in_floodplain = True

        for match in matches:
            _absorb_match(result, match, seen_layers)

    if not result.zoning_code:
        raise MunicipalZoningUnavailableError(
            f'No Austin zoning polygon covers {address!r} (likely outside city zoning jurisdiction)'
        )

    if result.in_floodplain:
        result.restrictions.insert(0, ZoningRestriction(
            label='FEMA/City floodplain',
            detail='Parcel intersects a mapped floodplain',
            severity='critical',
        ))

    result.restrictions.sort(key=lambda r: {'critical': 0, 'high': 1, 'info': 2}.get(r.severity, 3))
    return result


def _absorb_match(result, match, seen_layers):
    """Fold one identify result into the accumulating MunicipalZoningResult."""
    layer = match.get('layerName') or ''
    attrs = match.get('attributes') or {}

    if layer in ('Zoning', 'Zoning Text'):
        # The full string, suffixes included (e.g. 'SF-3-HD-NCCD-NP') —
        # strictly more informative than RentCast's bare base code.
        result.zoning_code = result.zoning_code or _clean(attrs.get('Zoning'))
        return

    if layer == 'Zoning Ordinance':
        number = _clean(attrs.get('Ordinance Number'))
        if number and not any(o['number'] == number for o in result.ordinances):
            result.ordinances.append({
                'number': number,
                'url': safe_external_url(attrs.get('Ordinance hyperlink')),
            })
        return

    if layer == 'Zoning Case Managers':
        result.case_manager = {
            'name': _clean(attrs.get('Zoning Case Manager')),
            'phone': _clean(attrs.get('Phone Number')),
        }
        return

    if layer == 'Jurisdictions (No Fill)':
        result.jurisdiction = _clean(attrs.get('Jurisdiction'))
        return

    if layer in _IGNORED_LAYERS or not layer:
        return

    # An overlay/designation layer. Its mere presence is the finding; the
    # attributes just add specificity (which district, which document).
    if layer in seen_layers:
        return
    seen_layers.add(layer)

    # Deliberately not surfacing any MAX_HEIGHT attribute found here. The
    # only layer that carries one is the ETOD overlay, where it's the
    # height available *if* you opt into a density-bonus program — not a
    # cap. Reporting it as "max height" would overstate what a rebuild
    # gets by right (base SF-3 is 35 ft against ETOD's 90 ft). The
    # by-right height table lives in Land Development Code 25-2-492,
    # which isn't machine-readable — see this module's docstring.
    severity = 'critical' if layer in _CRITICAL_LAYERS else 'high' if layer in _HIGH_LAYERS else 'info'
    result.restrictions.append(ZoningRestriction(
        label=layer,
        detail=_first_detail(attrs),
        severity=severity,
        url=safe_external_url(attrs.get('Ordinance hyperlink')) or safe_external_url(attrs.get('Hyperlink URL')),
    ))


# Attribute names worth showing as an overlay's detail, most specific first.
_DETAIL_KEYS = (
    'Sub Name', 'ZONING_OVERLAY_SUB_NAME', 'ZONING_OVERLAY_NAME', 'Name',
    'Ordinance Number', 'SOURCE_DOCUMENT', 'Source Document',
)


def _first_detail(attrs):
    for key in _DETAIL_KEYS:
        value = _clean(attrs.get(key))
        if value:
            return value
    return ''


def safe_external_url(value):
    """
    The value if it is an http(s) URL, otherwise ''.

    Every URL here comes from a third party's GIS attributes, and under
    discovery the publishing account can be anyone. Jinja's autoescaping
    does not neutralise a `javascript:` or `data:` URI inside an href —
    it escapes the quoting, not the scheme — so an attribute like
    "Hyperlink URL": "javascript:..." renders as a live link on the
    signed-in user's own analysis page. Restricting to http(s) is what
    stops that; callers render plain text when this returns ''.

    Applied both when ingesting GIS attributes and again at render time.
    The second pass isn't redundant: zoning detail is cached indefinitely
    by design, so rows written before this existed would otherwise keep
    serving whatever they stored.
    """
    if not isinstance(value, str):
        return ''
    candidate = value.strip()
    return candidate if candidate.lower().startswith(('http://', 'https://')) else ''


def _clean(value):
    if value in (None, 'Null', 'null'):
        return ''
    return str(value).strip()


def _geocode_austin(address, session):
    try:
        response = _get(session, AUSTIN_GEOCODE_URL, {
            'SingleLine': address,
            'outSR': 4326,
            'f': 'json',
        })
    except requests.RequestException as exc:
        raise MunicipalZoningUnavailableError(f'Austin geocoder request failed for {address!r}: {exc}') from exc

    candidates = response.get('candidates') or []
    best = candidates[0] if candidates else None
    if not best or best.get('score', 0) < AUSTIN_MIN_GEOCODE_SCORE:
        raise MunicipalZoningUnavailableError(f'Austin geocoder found no confident match for {address!r}')

    return best['location']


def _identify(session, service, location):
    """
    One point-in-polygon query against every layer of a MapServer at once
    (ArcGIS `identify`), rather than a query per layer. mapExtent and
    imageDisplay are required by the API even though nothing is being
    drawn — the tolerance is in pixels of that notional image, so a small
    box around the point keeps the match tight to the parcel.
    """
    lon, lat = location['x'], location['y']
    payload = _get(session, f'{AUSTIN_BASE}/{service}/MapServer/identify', {
        'geometry': f'{lon},{lat}',
        'geometryType': 'esriGeometryPoint',
        'sr': 4326,
        'layers': 'all',
        'tolerance': 2,
        'mapExtent': f'{lon - 0.01},{lat - 0.01},{lon + 0.01},{lat + 0.01}',
        'imageDisplay': '800,600,96',
        'returnGeometry': 'false',
        'f': 'json',
    })
    return payload.get('results') or []


def _get(session, url, params):
    response = session.get(url, params=params, timeout=DEFAULT_TIMEOUT, headers={
        'Accept': 'application/json',
        'User-Agent': USER_AGENT,
    })
    response.raise_for_status()
    return response.json()
