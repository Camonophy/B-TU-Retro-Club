"""
Utility functions for Kleinanzeigen Scraper
"""

import json
import random
import re
import time
from datetime import datetime, timedelta
from typing import Optional, List, Dict
from urllib.parse import urlparse, parse_qs, urlencode, urlunparse
from dateutil.parser import parse as parse_date
from dateutil.relativedelta import relativedelta

import requests
from bs4 import BeautifulSoup

from .models import Listing
from config.settings import Settings


# ---------------------------------------------------------------------------
# HTML parser
# ---------------------------------------------------------------------------
def get_parser():
    """
    Get the best available HTML parser for BeautifulSoup.
    Tries lxml first, falls back to html.parser.
    """
    try:
        import lxml  # noqa: F401
        return "lxml"
    except ImportError:
        return "html.parser"


def get_random_user_agent() -> str:
    """Get a random user agent from settings"""
    return random.choice(Settings.USER_AGENTS)


# ---------------------------------------------------------------------------
# URL generation
# ---------------------------------------------------------------------------
def _region_url(slug: str, location_id: Optional[str]) -> str:
    """
    Build a region-filtered base URL for the Immobilien umbrella category.

    The locationId query parameter (encoded in the URL path as
    `<category>l<locationId>`) is what actually scopes the search to the
    requested Bundesland on modern Kleinanzeigen. Without it, the search
    returns nationwide results.
    """
    base = Settings.BASE_URL.rstrip("/")
    url = f"{base}/s-immobilien/{slug}/{Settings.IMMOBILIEN_CATEGORY}"
    if location_id:
        url += f"l{location_id}"
    return url


def generate_search_url(bundesland_url_param: str, page: int = 1,
                        location_id: Optional[str] = None) -> str:
    """
    Generate Kleinanzeigen search URL for the Immobilien umbrella category,
    region-filtered via the locationId parameter.
    """
    url = _region_url(bundesland_url_param, location_id)
    sep = "?" if "?" not in url else "&"
    url = f"{url}{sep}{Settings.DEFAULT_QUERY_PARAMS}"
    if page > 1:
        url = f"{url}&o={page}"
    return url


def generate_all_category_urls(bundesland_url_param: str,
                               location_id: Optional[str] = None) -> List[str]:
    """
    Generate search URLs for every configured real estate subcategory.

    Each URL uses the modern category-code scheme:
        /s-immobilien/<region>/c<code>l<locationId>?<default params>

    The locationId is required for the region filter to work, and the
    DEFAULT_QUERY_PARAMS (posterType=PRIVATE & sortingField=SORTING_DATE)
    ensure we surface old private-seller listings, not commercial ones.
    """
    base = Settings.BASE_URL.rstrip("/")
    urls = []
    for cat in Settings.REAL_ESTATE_SUBCATEGORIES:
        url = f"{base}/s-immobilien/{bundesland_url_param}/{cat['code']}"
        if location_id:
            url += f"l{location_id}"
        sep = "?" if "?" not in url else "&"
        url = f"{url}{sep}{Settings.DEFAULT_QUERY_PARAMS}"
        urls.append(url)
    return urls


def build_page_url(base_url: str, page: int) -> str:
    """
    Build a paginated URL from a category base URL.

    Uses ?o=N, which works on the modern site. Appends the page number
    alongside any existing query string.
    """
    if page <= 1:
        return base_url

    parsed = urlparse(base_url)
    qs = parse_qs(parsed.query)
    qs["o"] = [str(page)]
    new_query = urlencode(qs, doseq=True)
    return urlunparse(parsed._replace(query=new_query))


def build_subcategory_url(category_code: str, location_id: str,
                          page: int = 1) -> str:
    """
    Build a URL for a single subcategory at a single sub-location.

    Used by the sub-location walker, which iterates through every city
    inside a Bundesland. Example:
        build_subcategory_url("c208", "1387")  -> Rees Häuser zum Kauf
    """
    base = Settings.BASE_URL.rstrip("/")
    url = f"{base}/s-immobilien/{category_code}l{location_id}"
    sep = "?"
    url = f"{url}{sep}{Settings.DEFAULT_QUERY_PARAMS}"
    if page > 1:
        url = f"{url}&o={page}"
    return url


# ---------------------------------------------------------------------------
# Sub-location discovery
# ---------------------------------------------------------------------------
def fetch_sub_locations(bundesland_url_param: str,
                        location_id: str,
                        session: Optional[requests.Session] = None,
                        delay: float = 0.5) -> List[Dict[str, str]]:
    """
    Walk the search page for the requested Bundesland and collect every
    sub-location (city) linked from the sidebar.

    Each entry is {"id": "<locationId>", "name": "<city name>"}. The state
    itself is excluded.

    Args:
        bundesland_url_param: URL slug for the Bundesland.
        location_id: Numeric locationId for the Bundesland.
        session: Optional requests.Session (created if None).
        delay: Seconds to sleep before the request.

    Returns:
        List of sub-location dicts. Empty on any error.
    """
    if session is None:
        session = requests.Session()
        session.headers.update(Settings.DEFAULT_HEADERS)
        session.headers.update({"User-Agent": get_random_user_agent()})

    # Use the umbrella category page; the sidebar is the same on every category.
    url = (f"https://www.kleinanzeigen.de/s-immobilien/{bundesland_url_param}/"
           f"{Settings.IMMOBILIEN_CATEGORY}l{location_id}"
           f"?{Settings.DEFAULT_QUERY_PARAMS}")

    time.sleep(delay)
    try:
        r = session.get(url, timeout=Settings.REQUEST_TIMEOUT)
        if r.status_code != 200:
            return []
        soup = BeautifulSoup(r.text, get_parser())

        seen: Dict[str, str] = {}

        # Sub-locations appear in an unclassed <section> as links like
        #   /s-haus-kaufen/<city-slug>/anbieter:privat/c208l<id>
        # or  /s-immobilien/<city>/c<N>l<id>
        for link in soup.select("a[href*='l'][href*='c']"):
            href_str = str(link.get("href", "") or "")
            text = str(link.get_text(strip=True) or "")
            # Skip property-type facets (e.g. "Einfamilienhaus freistehend")
            if not text or len(text) > 30:
                continue
            if any(w in text for w in
                   ["Haus", "Wohnung", "freistehend", "Reihen",
                    "Mehrfamilienhaus", "Bungalow", "Bauern",
                    "Doppel", "Villa", "Andere", "Stockwerk",
                    "Zimmer", "Etagen", "Baujahr", "Denkmalschutz",
                    "Kategorien", "Alle", "Immobilien"]):
                continue
            # Extract a 3+ digit locationId
            m = re.search(r"l(\d{3,})", href_str)
            if not m:
                continue
            lid = m.group(1)
            if lid == location_id:    # skip the state itself
                continue
            seen[lid] = text

        return [{"id": lid, "name": name} for lid, name in sorted(seen.items(),
                                                                  key=lambda x: x[1])]
    except Exception:
        return []


# ---------------------------------------------------------------------------
# Date parsing
# ---------------------------------------------------------------------------
def parse_kleinanzeigen_date(date_str: str) -> Optional[datetime]:
    """Parse a Kleinanzeigen date string into a datetime object."""
    if not date_str:
        return None

    date_str = date_str.strip().lower()
    # Normalise the common "Heute, 19:51" / "Gestern, 09:42" form to
    # just the keyword so the relative-time branches below can match.
    for kw in ("heute", "gestern"):
        if date_str == kw or date_str.startswith(kw + ",") or date_str.startswith(kw + " "):
            return datetime.now() - timedelta(days=1) if kw == "gestern" else datetime.now()

    # Handle "Heute" (Today)
    if date_str == "heute":
        return datetime.now()

    # Handle "Gestern" (Yesterday)
    if date_str == "gestern":
        return datetime.now() - timedelta(days=1)

    match = re.match(r"vor (\d+) tagen?", date_str)
    if match:
        return datetime.now() - timedelta(days=int(match.group(1)))
    match = re.match(r"vor (\d+) wochen?", date_str)
    if match:
        return datetime.now() - timedelta(weeks=int(match.group(1)))
    match = re.match(r"vor (\d+) monaten?", date_str)
    if match:
        return datetime.now() - relativedelta(months=int(match.group(1)))
    match = re.match(r"vor (\d+) jahren?", date_str)
    if match:
        return datetime.now() - relativedelta(years=int(match.group(1)))

    match = re.match(r"(\d{1,2})\.(\d{1,2})\.(\d{4})", date_str)
    if match:
        day, month, year = match.groups()
        try:
            return datetime(int(year), int(month), int(day))
        except ValueError:
            pass

    match = re.match(r"(\d{1,2})\.(\d{4})", date_str)
    if match:
        month, year = match.groups()
        try:
            return datetime(int(year), int(month), 1)
        except ValueError:
            pass

    month_names = {
        "januar": 1, "jan": 1,
        "februar": 2, "feb": 2,
        "märz": 3, "maerz": 3, "mar": 3,
        "april": 4, "apr": 4,
        "mai": 5,
        "juni": 6, "jun": 6,
        "juli": 7, "jul": 7,
        "august": 8, "aug": 8,
        "september": 9, "sep": 9, "sept": 9,
        "oktober": 10, "okt": 10, "oct": 10,
        "november": 11, "nov": 11,
        "dezember": 12, "dez": 12, "dec": 12,
    }
    match = re.match(r"([a-zäöü]+) (\d{4})", date_str)
    if match:
        month_name, year = match.groups()
        if month_name in month_names:
            try:
                return datetime(int(year), month_names[month_name], 1)
            except ValueError:
                pass

    try:
        return parse_date(date_str, dayfirst=True, yearfirst=False)
    except (ValueError, TypeError):
        return None


def calculate_age_days(date_str: str) -> Optional[int]:
    """Calculate age in days from a Kleinanzeigen date string."""
    parsed = parse_kleinanzeigen_date(date_str)
    if parsed:
        return (datetime.now() - parsed).days
    return None


def is_older_than_3_months(date_str: str) -> bool:
    """Check if a listing is older than 3 months (90 days)."""
    age = calculate_age_days(date_str)
    return age is not None and age > 90


def extract_listing_id(url: str) -> Optional[str]:
    """Extract listing ID from a Kleinanzeigen URL."""
    if not url:
        return None
    match = re.search(r"/s-anzeige/[^/]+/(\d+)", url)
    return match.group(1) if match else None


def normalize_price(price_str: Optional[str]) -> Optional[str]:
    """Normalize a price string (strip currency symbols, collapse whitespace)."""
    if not price_str:
        return None
    cleaned = re.sub(r"[€$\s]", "", price_str).strip()
    cleaned = cleaned.replace(",", ".")
    return cleaned or None


# ---------------------------------------------------------------------------
# Structured price parsing
# ---------------------------------------------------------------------------
# What we see in the wild (verified against the latest NRW export):
#   "184.500 €"                       -> 184500, vb=False
#   "1 € VB"                          -> 1,     vb=True
#   "215.000 € VB 265.000 €"          -> 215000, vb=True   (lower price wins)
#   "550 € 700 €"                     -> 550,    vb=False  (lower wins)
#   "150 € VB"                        -> 150,    vb=True
#   "43.900 € VB 59.000 €"            -> 43900,  vb=True
#   "VB"                              -> None,  vb=True
#   "" / None                         -> None,  vb=False
#
# Kleinanzeigen uses '.' as the thousands separator and never as a decimal
# separator for prices (cents are never shown). Numbers like "1.000" mean
# one thousand euros. We strip the '.', then parse as int.
#
# We accept THREE shapes:
#   1) "43.900"          - thousands-grouped, e.g. 43900
#   2) "150", "1", "700" - plain integers
#   3) "150000"          - already-concatenated integers (rare; defensive)
# The hundreds-grouped pattern is tried first so "1.500" becomes 1500,
# not 1 and 500. The plain integer pattern is then applied to anything
# that wasn't already matched.
_PRICE_THOUSAND_RE = re.compile(r"\d{1,3}(?:\.\d{3})+")
# Plain integers — 1+ digits. Matched only on spans NOT already
# claimed by a thousands-grouped match. The conservative regex (no
# word boundary tricks) avoids spuriously matching dates or zip codes
# because we already exclude those spans.
_PRICE_INT_RE = re.compile(r"\d+")


def _extract_price_numbers(price_str: str) -> List[int]:
    """Return every numeric value present in a price string, as integer euros.

    Handles German thousands-separator notation ("43.900") and ignores
    anything that isn't a number sequence.
    """
    if not price_str:
        return []
    out: List[int] = []
    consumed_spans: List[tuple[int, int]] = []
    # First pass: thousands-grouped numbers (highest priority).
    for raw in _PRICE_THOUSAND_RE.findall(price_str):
        try:
            out.append(int(raw.replace(".", "")))
            consumed_spans.append(
                (price_str.index(raw), price_str.index(raw) + len(raw))
            )
        except ValueError:
            continue
    # Second pass: plain integer >= 10 that was NOT inside a
    # thousands-grouped match (so "43.900 VB 59.000" doesn't also
    # yield 43 and 59).
    for m in _PRICE_INT_RE.finditer(price_str):
        start, end = m.span()
        if any(s <= start and end <= e for s, e in consumed_spans):
            continue
        out.append(int(m.group(0)))
    return out


def parse_price_eur(price_str: Optional[str]) -> tuple[Optional[int], bool]:
    """Parse a Kleinanzeigen price string into (lowest_number_or_None, has_vb).

    - Returns the LOWER of any numbers found, since a "X € VB Y €" pattern
      means "asking X, would also accept Y" and the exclusion rule cares
      about the lower bound.
    - Returns has_vb=True when "VB" (Verhandlungsbasis / negotiable)
      appears in the string, otherwise False.
    - Returns (None, has_vb) when no number is present.
    """
    if not price_str:
        return None, False
    has_vb = bool(re.search(r"\bvb\b", price_str, flags=re.IGNORECASE))
    nums = _extract_price_numbers(price_str)
    if not nums:
        return None, has_vb
    return min(nums), has_vb


def parse_price_pair_eur(price_str: Optional[str]) -> tuple[Optional[int], Optional[int], bool]:
    """Parse a Kleinanzeigen price string into (lower, higher, has_vb).

    - `lower`: the lowest number in the cell (same as parse_price_eur[0]).
    - `higher`: the highest number in the cell, or None if there is only
      one number. Used to populate the "Vorheriger Wert (€)" column when
      the search card shows two prices (e.g. "215.000 € VB 265.000 €" or
      "550 € 700 €"). Per user spec 2026-07-03: when there was no
      previous price, the cell is empty (None), NOT a repeated value.
    - `has_vb`: True iff "VB" appears in the string.

    Examples (matches the user's spec):
      "184.500 €"             -> (184500, None, False)   single price
      "1 €"                   -> (1,     None, False)   single price
      "215.000 € VB 265.000 €"-> (215000, 265000, True)  two prices with VB
      "43.900 € VB 59.000 €"  -> (43900,  59000,  True)  two prices with VB
      "550 € 700 €"           -> (550,    700,    False) two prices no VB
      "VB"                    -> (None,   None,   True)  VB alone
      None / ""               -> (None,   None,   False) missing
    """
    if not price_str:
        return None, None, False
    has_vb = bool(re.search(r"\bvb\b", price_str, flags=re.IGNORECASE))
    nums = _extract_price_numbers(price_str)
    if not nums:
        return None, None, has_vb
    if len(nums) == 1:
        return nums[0], None, has_vb
    return min(nums), max(nums), has_vb


# ---------------------------------------------------------------------------
# "For sale" detection
# ---------------------------------------------------------------------------
# Heuristic signals for WANTED (looking for real estate) vs. FOR-SALE listings.
# These are matched case-insensitively against the title text. Order matters:
# a sell signal wins over a wanted signal if both appear in the same title,
# because real listings sometimes say "Suche Nachmieter" inside an offer
# description.
_WANTED_TITLE_PATTERNS = [
    r"\bsuche\b",
    r"\bgesucht\b",
    r"\bsuch-\b",
    r"\bbewerber\b",
    r"\bbewerbung\b",
    r"\btausche\b",
    r"\btausch\b",
    r"\bsuchend\b",
    r"\blooking\s+for\b",
]
_SELL_TITLE_PATTERNS = [
    r"\bverkaufe\b",
    r"\bzu\s+verkaufen\b",
    r"\bzu\s+verkaufe?n?\b",
    r"\bzu\s+vermieten\b",
    r"\bbiete\b",
    r"\bzu\s+kaufen\b",
    r"\bkauf\s+angebot\b",
    r"\bverkauf\b",
]


def is_for_sale_listing(title: str) -> bool:
    """Return True if the listing title indicates an OFFER (not a request).

    Rules:
      * If a SELL signal appears, return True (it's an offer).
      * Else if a WANTED signal appears, return False (it's a request).
      * Else return True (ambiguous — keep it; the price/title filter will
        catch most remaining non-offers).
    """
    if not title:
        return True
    t = title.lower()
    for pat in _SELL_TITLE_PATTERNS:
        if re.search(pat, t):
            return True
    for pat in _WANTED_TITLE_PATTERNS:
        if re.search(pat, t):
            return False
    return True

def is_excluded_by_price(price_str: Optional[str],
                         min_price_eur: Optional[int] = None) -> bool:
    """
    Return True if the listing must be excluded by the price rule.

    Rule (as of 2026-07-03, per user request): keep only listings whose
    lowest numeric price is strictly above ``min_price_eur`` EUR.
    Anything at or below that floor — VB or no VB — is dropped, as is
    any listing whose price cell is missing or unparseable.

    ``min_price_eur`` defaults to ``Settings.MIN_PRICE_EUR`` (150 000)
    when not passed. The caller in ``_parse_listing_card`` does not pass
    it, so production behaviour follows ``Settings.MIN_PRICE_EUR``.
    Tests pass a small value (e.g. 100) to exercise boundary cases
    without depending on a real >150 000 € fixture.

    Notes on the rule:
      * No price present at all → exclude.
      * "VB" present with no number → exclude (same as no price).
      * Two prices like "150.001 € VB 200.000 €" → use the LOWER
        (150.001) and keep (above 150 000).
      * Two prices like "120.000 € VB 180.000 €" → use the LOWER
        (120.000) and exclude (≤ 150 000).
      * "1 €" with no VB → exclude (1 ≤ 150 000). This is intentional:
        the user explicitly wants sub-floor listings removed whether
        or not VB is present.
    """
    if min_price_eur is None:
        min_price_eur = Settings.MIN_PRICE_EUR
    if not price_str or not price_str.strip():
        return True
    value, _has_vb = parse_price_eur(price_str)
    if value is None:
        return True           # missing or unparseable price
    if value <= min_price_eur:
        return True
    return False


def normalize_location(location_str: Optional[str]) -> Optional[str]:
    """Normalize a location string."""
    if not location_str:
        return None
    return location_str.replace("\u200b", "").strip() or None


# ---------------------------------------------------------------------------
# Detail-page date fetching
# ---------------------------------------------------------------------------
_DATE_PATTERN = re.compile(
    r"(Heute|Gestern|vor \d+ (?:Tagen|Wochen|Monaten|Jahren)|\d{1,2}\.\d{1,2}\.\d{4})"
)


# ----------------------------------------------------------------------
# Detail-page seller-name fetching
# ----------------------------------------------------------------------
def fetch_listing_seller_name(listing_url: str, session=None) -> Optional[str]:
    """
    Fetch the display name of the seller from a Kleinanzeigen detail page.

    The seller is shown in the user-profile section on the right side of
    the detail page. The simplest reliable selector is `[class*="user"]`
    (matches `.user-profile-section`, `.user-badge-container`, etc.). We
    pick the element whose text is the seller's full name (e.g.
    "Monika Möller") rather than nick badge ("MM") or ratings ("TOP").

    Returns the trimmed name, or None if the page couldn't be fetched or
    no name was found.
    """
    import requests as _requests

    if not listing_url:
        return None

    if not listing_url.startswith("http"):
        listing_url = f"{Settings.BASE_URL}{listing_url}"

    try:
        if session:
            sess = session
        else:
            sess = _requests.Session()
            sess.headers.update(Settings.DEFAULT_HEADERS)
            sess.headers.update({"User-Agent": get_random_user_agent()})

        time.sleep(random.uniform(0.3, 0.8))

        response = sess.get(listing_url, timeout=Settings.REQUEST_TIMEOUT)
        if response.status_code != 200:
            return None

        soup = BeautifulSoup(response.text, get_parser())

        # Preferred selector: any element in the user-section class that
        # carries a Person name. JSON-LD <script type="application/ld+json">
        # sometimes contains a Person schema with a `name` field — try that
        # first since it's the cleanest source.
        for s in soup.select('script[type="application/ld+json"]'):
            if not s.string:
                continue
            try:
                import json as _json
                data = _json.loads(s.string)
            except Exception:
                continue
            # Could be a list, dict, or graph
            candidates = data if isinstance(data, list) else [data]
            for entry in candidates:
                if not isinstance(entry, dict):
                    continue
                # @graph containing the seller
                graph = entry.get("@graph", [])
                if isinstance(graph, list):
                    candidates.extend(graph)
            for entry in candidates:
                if not isinstance(entry, dict):
                    continue
                if entry.get("@type") in ("Person", "person"):
                    name = entry.get("name")
                    if isinstance(name, str) and len(name.strip()) >= 2:
                        return name.strip()

        # Fallback: scan [class*="user"] for the most "name-like" text.
        # The seller's name is typically the first inner element with
        # text content of 2+ words.
        user_section = soup.select_one("[class*='user-profile-section']") \
                       or soup.select_one("[class*='user-info']") \
                       or soup
        for el in user_section.select("[class*='user']"):
            txt = el.get_text(" ", strip=True)
            # Skip the avatar initials (e.g. "MM"), ratings ("TOP"),
            # and "Privater Nutzer" labels
            if not txt or len(txt) < 3:
                continue
            if txt in {"MM", "Privater Nutzer", "Gewerblicher Verkäufer"}:
                continue
            if txt.startswith(("TOP", "Freundlich", "Zuverlässig", "Aktiv seit")):
                continue
            # Names typically have a capitalised first letter and may have
            # a space; reject anything that's clearly a category label.
            if txt[0].isupper() and not txt[0].isdigit():
                return txt

        return None

    except Exception:
        return None


def fetch_listing_date(listing_url: str, session=None) -> Optional[str]:
    """
    Fetch the listing activation date from a Kleinanzeigen detail page.

    The only date present in the static HTML is in the
    `boxedarticle--details--full` section: a calendar icon followed by a
    `<span>` with the date. This date represents the LAST ACTIVATION
    of the listing — i.e. the day the seller last bumped it. For a buyer
    looking for listings that have been sitting around for 3+ months
    without a seller refresh, that IS the signal they want.

    Skips the seller's "Aktiv seit DD.MM.YYYY" date in the user profile
    (which is the seller's account age, not the listing's).
    """
    import requests as _requests

    if not listing_url:
        return None

    if not listing_url.startswith("http"):
        listing_url = f"{Settings.BASE_URL}{listing_url}"

    try:
        if session:
            sess = session
        else:
            sess = _requests.Session()
            sess.headers.update(Settings.DEFAULT_HEADERS)
            sess.headers.update({"User-Agent": get_random_user_agent()})

        time.sleep(random.uniform(0.3, 0.8))

        response = sess.get(listing_url, timeout=Settings.REQUEST_TIMEOUT)
        if response.status_code != 200:
            return None

        soup = BeautifulSoup(response.text, get_parser())

        # Pattern 1 (most reliable): the calendar icon inside the
        # `boxedarticle--details--full` block.
        container = soup.select_one("#viewad-extra-info")
        if container:
            span = container.select_one("span")
            if span:
                text = span.get_text(strip=True)
                if text and _DATE_PATTERN.search(text):
                    return text

        # Pattern 2: any calendar icon followed by a date span, scoped
        # to the main article to avoid seller profile noise.
        article = soup.select_one("article.boxedarticle") or soup.select_one("article")
        if article:
            cal_icon = article.select_one("i.icon-calendar-gray-simple")
            if cal_icon:
                nxt = cal_icon.find_next("span")
                if nxt:
                    text = nxt.get_text(strip=True)
                    if _DATE_PATTERN.search(text):
                        return text

        # Pattern 3 (last resort): first date pattern in the article
        # that is NOT the seller's "Aktiv seit" date.
        if article:
            text = article.get_text("\n", strip=True)
            lines = text.split("\n")
            for line in lines:
                line = line.strip()
                if not line or line.lower().startswith("aktiv seit"):
                    continue
                m = _DATE_PATTERN.search(line)
                if m:
                    return m.group(1)

        return None

    except Exception:
        return None