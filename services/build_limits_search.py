"""
Saved results of on-demand searches for a zoning district's exact build
limits, and the checks that decide which reported numbers may be saved.

Hand-checked towns and the National Zoning Atlas
(services/dimensional_standards.py) cover a small part of the country.
Everywhere else, a search reads the town's own zoning code and saves what
it found here, per (jurisdiction, district), so every analysis in that
district shows it without paying for it again.

A number is kept only when all three checks pass:
  1. its quote contains that number,
  2. the quote appears word for word in text actually received from the
     page it cites during the search: the page text returned by web
     fetch, a fetched PDF's extracted text, or a search-result excerpt,
     and
  3. the quote doesn't name units other than the one it was recorded in.
Whatever reports the limits also writes the quote, so the first check
alone would only prove it agrees with itself. The second ties it to what
the page says, and the third catches a number lifted from the wrong line
of a dimensional table, where a coverage percentage could otherwise be
saved as a height in feet. Anything that fails is dropped, so a table
can come back partial but never with a number its source doesn't
contain. Which line of a table a number came from is still only as good
as the search: a quote that names no unit at all can't be placed.

The search that produces these reports arrives with it.
"""

import base64
import io
import logging
import re
import string
import unicodedata
from urllib.parse import urlsplit

import pypdf

from integrations.municipal_zoning import safe_external_url
from models import BuildLimitsLookup

logger = logging.getLogger(__name__)


MAX_PDF_PAGES = 500
MIN_QUOTE_CHARS = 12
MAX_QUOTE_CHARS = 400

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


def verify_limits(reported, evidence):
    """(kept, dropped): each reported limit either passes every check or is dropped with the reason."""
    kept, dropped, seen = [], [], set()
    for item in reported:
        if not isinstance(item, dict):
            continue
        key, value, unit = item.get('key'), item.get('value'), item.get('unit')
        # One over the limit, so an over-long quote still fails the length
        # check below rather than being silently trimmed into passing it.
        quote = _clip(item.get('quote'), MAX_QUOTE_CHARS + 1)
        url = safe_external_url(item.get('source_url'))
        reason = _rejection(key, value, unit, quote, url, evidence, seen)
        if reason:
            number = value if isinstance(value, (int, float)) and not isinstance(value, bool) else None
            dropped.append({'key': str(key)[:40], 'value': number, 'reason': reason})
            continue
        seen.add(key)
        kept.append({
            'key': key, 'value': int(value) if value == int(value) else float(value), 'unit': unit,
            'quote': ' '.join(quote.split()), 'source_url': url, 'section': _clip(item.get('section'), 120),
        })
    return kept, dropped


def _rejection(key, value, unit, quote, url, evidence, seen):
    # Checked before the lookup: an unhashable key would raise here rather
    # than be dropped the way every other malformed field is.
    if not isinstance(key, str) or key not in LIMITS:
        return 'unknown limit'
    if key in seen:
        return 'reported twice'
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not 0 < value < 10_000_000:
        return 'no usable number'
    if unit not in UNITS:
        return 'unknown unit'
    if not MIN_QUOTE_CHARS <= len(quote) <= MAX_QUOTE_CHARS:
        return 'quote too short or too long'
    if not url:
        return 'no source link'
    if not _quote_has_number(quote, value):
        return "quote doesn't contain the number"
    mentioned = _unit_mentions(quote)
    if mentioned and unit not in mentioned:
        return 'quote names different units'
    if not evidence.contains(url, quote):
        return 'quote not found on the page it cites'
    return None


class _Evidence:
    """Text actually received from each page during the search, for checking quotes against."""

    def __init__(self):
        self.pages = {}  # url key -> [squashed text]

    def add(self, content):
        blocks = list(content or [])
        requested = _requested_urls(blocks)
        for block in blocks:
            kind = getattr(block, 'type', '')
            if kind == 'web_fetch_tool_result':
                result = getattr(block, 'content', None)
                if getattr(result, 'type', '') != 'web_fetch_result':
                    continue
                document = getattr(result, 'content', None)
                text = _source_text(getattr(document, 'source', None))
                self._add(getattr(result, 'url', ''), text)
                # A fetch reports the url it landed on, but the quote cites
                # the one it asked for. Town code sites redirect often, and
                # keeping only the final url would drop every limit from a
                # page that moved, as if the search had made them up.
                self._add(requested.get(getattr(block, 'tool_use_id', ''), ''), text)
            elif kind == 'text':
                # Search-result excerpts the API attached to Claude's text.
                # The API writes cited_text, not Claude, so it counts.
                for citation in getattr(block, 'citations', None) or []:
                    if getattr(citation, 'type', '') == 'web_search_result_location':
                        self._add(getattr(citation, 'url', ''), getattr(citation, 'cited_text', ''))

    def contains(self, url, quote):
        needle = _squash(quote)
        return bool(needle) and any(needle in text for text in self.pages.get(_url_key(url), ()))

    def _add(self, url, text):
        if not (url and text):
            return
        squashed = _squash(text)
        texts = self.pages.setdefault(_url_key(url), [])
        # A fetch that didn't redirect files the same text under one key twice.
        if squashed not in texts:
            texts.append(squashed)


def _requested_urls(blocks):
    """tool_use_id -> the url each web fetch was asked for."""
    urls = {}
    for block in blocks:
        if getattr(block, 'type', '') != 'server_tool_use' or getattr(block, 'name', '') != 'web_fetch':
            continue
        params = getattr(block, 'input', None)
        url = params.get('url') if isinstance(params, dict) else None
        if url:
            urls[getattr(block, 'id', '')] = url
    return urls


def _source_text(source):
    kind = getattr(source, 'type', '')
    if kind == 'text':
        return getattr(source, 'data', '') or ''
    if kind == 'base64' and 'pdf' in (getattr(source, 'media_type', '') or ''):
        return _pdf_text(getattr(source, 'data', '') or '')
    return ''


def _pdf_text(data):
    try:
        reader = pypdf.PdfReader(io.BytesIO(base64.b64decode(data)))
        return '\n'.join(page.extract_text() or '' for page in reader.pages[:MAX_PDF_PAGES])
    except (pypdf.errors.PyPdfError, ValueError, KeyError, TypeError, AttributeError, OSError) as exc:
        # A PDF that won't parse just means no evidence from it.
        logger.info('Could not read fetched PDF: %s', exc)
        return ''


_PUNCTUATION = str.maketrans({
    '‘': "'", '’': "'", '“': '"', '”': '"', '‐': '-', '‑': '-', '‒': '-',
    '–': '-', '—': '-', '−': '-', '⁄': '/',
})


def _normalize(text):
    return unicodedata.normalize('NFKC', text or '').translate(_PUNCTUATION).lower()


def _squash(text):
    """Comparable form: case, curly quotes, dashes and all whitespace (line breaks in tables) ignored."""
    return re.sub(r'\s+', '', _normalize(text))


# How a zoning code writes each unit. Longest first where one contains
# another, so "square feet" is never read as plain feet.
_UNIT_WORDS = (
    ('sq ft', r"square\s+f(?:ee|oo)t|sq\.?\s*ft\.?"),
    ('acres', r"\bacres?\b"),
    ('%', r"%|\bper\s?cents?\b|\bpercent(?:age)?s?\b"),
    ('stories', r"\bstor(?:y|ies|ey|eys)\b"),
    ('ratio', r"\bratios?\b|\bf\.?a\.?r\.?\b"),
    ('ft', r"\bf(?:ee|oo)t\b|\bft\.?|\d\s*'"),
)


def _unit_mentions(quote):
    """
    The units the quote itself names, or nothing when it names none.

    The number checks say a quote contains the number and comes from the
    cited page; neither says the quote is about the limit it was filed
    under. A row lifted from the wrong line of a dimensional table keeps
    its own unit words, so a coverage percentage reported as a height in
    feet is catchable here. A quote that names no unit is left alone —
    plenty of real table rows don't repeat one.
    """
    text = _normalize(quote)
    mentions = set()
    for unit, pattern in _UNIT_WORDS:
        if re.search(pattern, text):
            mentions.add(unit)
            text = re.sub(pattern, ' ', text)
    return mentions


def _quote_has_number(quote, value):
    text = re.sub(r'\s+', ' ', re.sub(r'(?<=\d),(?=\d{3})', '', _normalize(quote)))
    plain = _plain(value)
    candidates = {plain}
    if 0 < value < 1:
        candidates.add(plain.removeprefix('0'))  # ".35"
    whole = int(value)
    if abs(value - whole - 0.5) < 1e-9:
        candidates |= {f'{whole} 1/2', f'{whole}1/2', f'{whole}-1/2'}  # "2½" normalizes to "21/2"
    return any(re.search(rf'(?<![\d.]){re.escape(c)}(?!\d|\.\d)', text) for c in candidates)


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


def _url_key(url):
    parts = urlsplit((url or '').strip())
    key = parts.netloc.lower().removeprefix('www.') + parts.path.rstrip('/')
    return f'{key}?{parts.query}' if parts.query else key


def _clip(value, limit):
    return value.strip()[:limit] if isinstance(value, str) else ''


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
