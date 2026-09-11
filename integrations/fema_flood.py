"""
FEMA flood zone for any US point, from the National Flood Hazard Layer.

Flood zones are the one build restriction published nationally,
authoritatively and in machine-readable form. FEMA's flood maps are the
regulatory basis for floodplain building rules and mandatory flood
insurance everywhere in the US. So unlike the rest of the zoning lookup,
which has to find each jurisdiction's own data, there's one source here
and it's the same for every address.

Verified against known areas while building this: Galveston (AO), Miami
Beach and Charleston (AE) come back inside the Special Flood Hazard Area;
Lexington MA and downtown Denver come back as Zone X, minimal hazard.

Informational only, like the rest of zoning. Any failure — FEMA down, an
unmapped area, an unexpected response — returns None rather than raising,
so it can never fail an analysis.
"""

import logging
from dataclasses import dataclass

import requests

logger = logging.getLogger(__name__)

NFHL_FLOOD_ZONES_URL = 'https://hazards.fema.gov/arcgis/rest/services/public/NFHL/MapServer/28/query'
DEFAULT_TIMEOUT = 10


@dataclass
class FloodZone:
    zone: str      # FEMA designation, e.g. 'AE', 'VE', 'AO', 'X', 'D'
    subtype: str   # e.g. 'AREA OF MINIMAL FLOOD HAZARD'; usually blank inside the SFHA
    in_sfha: bool  # inside the Special Flood Hazard Area, the regulatory "100-year" floodplain

    def as_dict(self):
        return {'zone': self.zone, 'subtype': self.subtype, 'in_sfha': self.in_sfha, 'source': 'FEMA NFHL'}


def lookup_flood_zone(lon, lat, session=None):
    """FloodZone at the point, or None if FEMA maps no zone there or can't be reached."""
    session = session or requests.Session()
    try:
        response = session.get(NFHL_FLOOD_ZONES_URL, params={
            'geometry': f'{lon},{lat}',
            'geometryType': 'esriGeometryPoint',
            'inSR': 4326,
            'spatialRel': 'esriSpatialRelIntersects',
            'outFields': 'FLD_ZONE,ZONE_SUBTY,SFHA_TF',
            'returnGeometry': 'false',
            'f': 'json',
        }, timeout=DEFAULT_TIMEOUT, headers={'Accept': 'application/json'})
        response.raise_for_status()
        payload = response.json()
    except (requests.RequestException, ValueError) as exc:
        logger.info('FEMA flood zone lookup failed at %s,%s: %s', lon, lat, exc)
        return None

    if not isinstance(payload, dict) or 'error' in payload:
        return None

    zones = [zone for zone in (_parse(feature) for feature in payload.get('features') or []) if zone]
    if not zones:
        return None
    # A point on a boundary can sit in more than one polygon. The more
    # hazardous one is what governs building there.
    return max(zones, key=lambda zone: zone.in_sfha)


def _parse(feature):
    attrs = feature.get('attributes') if isinstance(feature, dict) else None
    if not isinstance(attrs, dict):
        return None
    zone = str(attrs.get('FLD_ZONE') or '').strip()
    if not zone:
        return None
    return FloodZone(
        zone=zone,
        subtype=str(attrs.get('ZONE_SUBTY') or '').strip(),
        in_sfha=attrs.get('SFHA_TF') == 'T',
    )
