"""
Main scraping logic for Kleinanzeigen.de
"""

import dataclasses
import time
import random
import re
import logging
from typing import Optional, List, Dict
from datetime import datetime

import requests
from bs4 import BeautifulSoup
from requests.exceptions import RequestException

from .models import Listing, ScrapeResult
from .utils import (
    get_parser,
    parse_kleinanzeigen_date,
    get_random_user_agent,
    generate_all_category_urls,
    fetch_listing_date,
    fetch_listing_seller_name,
    build_page_url,
    build_subcategory_url,
    fetch_sub_locations,
    parse_price_eur,
    parse_price_pair_eur,
    is_for_sale_listing,
    is_excluded_by_price,
)
from config.settings import Settings
from . import ui as _ui


logger = logging.getLogger(__name__)


class KleinanzeigenScraper:
    """Main scraper class for Kleinanzeigen.de"""

    def __init__(self):
        self.session = requests.Session()
        self.settings = Settings()
        self._setup_session()

    # ------------------------------------------------------------------
    # Setup & low-level HTTP
    # ------------------------------------------------------------------
    def _setup_session(self):
        """Configure the requests session"""
        self.session.headers.update(self.settings.DEFAULT_HEADERS)
        self.session.headers.update({"User-Agent": get_random_user_agent()})

    def _get_random_delay(self) -> float:
        """Get a random delay between requests"""
        return random.uniform(*self.settings.REQUEST_DELAY)

    def _make_request(self, url: str, max_retries: Optional[int] = None) -> Optional[str]:
        """
        Make HTTP request with retries and rate limiting.

        Args:
            url: URL to request.
            max_retries: Maximum number of retries (defaults to settings).

        Returns:
            HTML content or None if failed.
        """
        if max_retries is None:
            max_retries = self.settings.MAX_RETRIES

        for attempt in range(max_retries + 1):
            try:
                delay = self._get_random_delay()
                logger.debug(f"Waiting {delay:.2f}s before request to {url}")
                time.sleep(delay)

                if attempt > 0:
                    self.session.headers.update({"User-Agent": get_random_user_agent()})

                response = self.session.get(url, timeout=self.settings.REQUEST_TIMEOUT)

                if response.status_code == 200:
                    return response.text
                if response.status_code == 429:
                    wait = 2 ** attempt * 5
                    logger.warning(f"Rate limited (429). Waiting {wait}s...")
                    time.sleep(wait)
                    # Loop will retry (this attempt counts toward max_retries)
                elif response.status_code in (403, 503):
                    # Cloudflare / Akamai challenge pages and explicit
                    # Service-Unavailable. Back off and retry.
                    wait = 2 ** attempt * 10
                    logger.warning(
                        f"Server-challenge/transient ({response.status_code}). "
                        f"Waiting {wait}s before retry {attempt + 1}/{max_retries + 1}..."
                    )
                    time.sleep(wait)
                elif response.status_code == 404:
                    logger.warning(f"Page not found: {url}")
                    return None
                elif response.status_code == 410:
                    # Kleinanzeigen returns 410 ("Gone") both for genuinely
                    # deleted listing URLs and as an anti-bot response
                    # during heavy scraping. For category sub-loc URLs
                    # (the URL shape we're seeing this on) the listing-page
                    # absolutely should exist — treat as transient and
                    # retry with exponential backoff.
                    wait = 2 ** attempt * 10
                    logger.warning(
                        f"HTTP 410 (transient?): {url} — "
                        f"waiting {wait}s before retry {attempt + 1}/{max_retries + 1}"
                    )
                    time.sleep(wait)
                else:
                    # Catch-all for 5xx, 408, etc.
                    logger.warning(f"HTTP {response.status_code}: {url}")

            except RequestException as e:
                logger.warning(f"Request failed (attempt {attempt + 1}/{max_retries + 1}): {e}")
                if attempt < max_retries:
                    time.sleep(2 ** attempt)

        logger.error(f"Failed to fetch {url} after {max_retries + 1} attempts")
        return None

    # ------------------------------------------------------------------
    # Parsing
    # ------------------------------------------------------------------
    def _parse_listing_card(self, card, parent_region: Optional[str] = None) -> Optional[Listing]:
        """
        Parse a single listing card from a search-results page.

        The current Kleinanzeigen DOM (verified 2026-06-28) uses these
        structural classes:
            <article class="aditem">
              <div class="aditem-main--top--left">  <!-- location -->
              <h2 class="text-module-begin">        <!-- title -->
                <a class="ellipsis" href="/s-anzeige/...">...</a>
              </h2>
              <p class="aditem-main--middle--price-shipping--price">  <!-- price -->
            </article>

        On sub-location search pages (e.g. c208l1387 for Rees) the card
        ALSO contains the activation date as a calendar-open icon followed
        by a date string in `.aditem-main--top--right`. On state-level pages
        the date is NOT in the card and must be fetched from the detail page.
        """
        try:
            # ---- URL (also gives us the title href) ----
            url = card.get("data-href")
            if not url:
                link = card.select_one("h2.text-module-begin a[href]") \
                       or card.select_one("a.ellipsis[href]") \
                       or card.select_one("a[href*='/s-anzeige/']")
                if link:
                    url = link.get("href")

            if not url:
                logger.debug("No URL on card, skipping")
                return None

            url = str(url)
            if not url.startswith("http"):
                url = f"{self.settings.BASE_URL}{url}"

            # ---- Title ----
            title_el = card.select_one("h2.text-module-begin a") \
                       or card.select_one("h2 a")
            title = title_el.get_text(strip=True) if title_el else ""

            # ---- Price ----
            price_el = card.select_one(".aditem-main--middle--price-shipping--price") \
                       or card.select_one("[class*='price-shipping--price']") \
                       or card.select_one(".price")
            price = price_el.get_text(" ", strip=True) if price_el else None
            if price:
                price = price.split("\n")[0].strip()

            # ---- Filter: "for sale" only ----
            # Some private sellers post wanted-ads (Suche / Gesucht /
            # Bewerber) in the same subcategories as real offers. Drop
            # them here so they never enter the dataset.
            if not is_for_sale_listing(title):
                logger.debug(f"Skip (wanted listing): {title!r}")
                return None

            # ---- Filter: price rule ----
            # Per user spec: exclude listings whose only price signal is
            # "VB" and less than 1000 €, or that have no price at all.
            if is_excluded_by_price(price):
                logger.debug(
                    f"Skip (price rule): {title!r} price={price!r}"
                )
                return None

            # Parse the price into structured numbers so the exporter can write
            # them as pure integers (no "€", no "VB"). Use the pair
            # variant so the second number (when present) becomes
            # `previous_price_eur` for the new "Vorheriger Wert (€)"
            # column. Single-price cells keep previous_price_eur=None.
            lower_value, higher_value, _has_vb = parse_price_pair_eur(price)
            price_eur = float(lower_value) if lower_value is not None else None
            previous_price_eur = (
                float(higher_value) if higher_value is not None else None
            )

            # ---- Location ----
            loc_el = card.select_one(".aditem-main--top--left") \
                       or card.select_one("[class*='top--left']") \
                       or card.select_one(".location")
            location = loc_el.get_text(" ", strip=True) if loc_el else None

            # The location text comes in two shapes:
            #   "46459 Rees"            (5-digit PLZ + city, the common case)
            #   "46459 Rees (3 km)"    (with distance marker, after strip)
            #   "Rees"                  (city only, no PLZ — rare, e.g. when
            #                            the listing is in a wider area)
            # We extract the 5-digit PLZ into its own field and keep the
            # city name in `location`.
            postleitzahl = ""
            if location:
                # Strip the unicode zero-width-space and any trailing
                # distance markers like "(6 km)" that Kleinanzeigen appends
                # for sub-location searches.
                location = location.replace("\u200b", "").strip()
                location = re.sub(r"\s*\(\d+\s*km\)\s*$", "", location).strip()
                # Collapse internal whitespace runs (from \n in the source).
                location = re.sub(r"\s+", " ", location)

                m = re.match(r"^(\d{5})\s+(.+)$", location)
                if m:
                    postleitzahl = m.group(1)
                    location = m.group(2).strip()
                else:
                    # No PLZ prefix — keep city name as-is, PLZ stays ""
                    pass

                # ---- Optional parent-region enrichment ----
                # If we know the parent region (from the page
                # breadcrumb, e.g. "Aachen") and the card-level
                # location is the child (e.g. "Aachen-Mitte"), join
                # them with " - " so the user sees the full
                # geographical context: "Aachen - Aachen-Mitte".
                #
                # We ALWAYS prefix when the parent is known; the
                # user's spreadsheet wants the explicit
                # "City - District" format even if the card already
                # shows "City-District" (which is ambiguous without
                # the parent prefix).
                if (parent_region
                    and parent_region.lower() != "kleinanzeigen"
                    and location
                    and location.lower() != parent_region.lower()):
                    location = f"{parent_region} - {location}"

            # ---- Activation date (present on every modern card) ----
            date_posted = None
            date_parsed = None
            date_right = card.select_one(".aditem-main--top--right")
            if date_right:
                text = date_right.get_text(" ", strip=True)
                # Two formats appear in the wild:
                #   - absolute: "13.06.2025" (sub-location cards)
                #   - relative: "Heute, 19:18" / "Gestern, 09:42" (state-level cards)
                # Both are accepted by parse_kleinanzeigen_date.
                # First try the absolute DD.MM.YYYY format (more precise).
                m = re.search(r"(\d{1,2}\.\d{1,2}\.\d{4})", text)
                if m:
                    date_posted = m.group(1)
                elif text:
                    # Fall back to the relative text (parse_kleinanzeigen_date
                    # handles "Heute", "Gestern", "vor X Tagen", etc.).
                    date_posted = text
                if date_posted:
                    date_parsed = parse_kleinanzeigen_date(date_posted)

            return Listing(
                title=title,
                url=url,
                price=price,
                price_eur=price_eur,
                previous_price_eur=previous_price_eur,
                location=location,
                date_posted=date_posted,
                date_parsed=date_parsed,
                postleitzahl=postleitzahl,
                # seller_name left as "" — populated later by
                # _fetch_seller_names().
            )

        except Exception as e:
            logger.error(f"Error parsing listing card: {e}")
            return None

    def _extract_listings_from_page(self, html: str) -> List[Listing]:
        """Extract every listing card from one search-results HTML page."""
        listings: List[Listing] = []
        try:
            soup = BeautifulSoup(html, get_parser())
            # Try modern first, then fall back gracefully.
            cards = soup.select(".aditem")
            if not cards:
                cards = soup.select("article")
            logger.debug(f"Found {len(cards)} potential listing cards")

            # Pull the parent region (city name) from the page
            # breadcrumb so we can format `Standort` as
            # "City - Sub-district".
            parent_region = self._extract_page_region(soup)

            for card in cards:
                listing = self._parse_listing_card(card, parent_region)
                if listing:
                    listings.append(listing)
        except Exception as e:
            logger.error(f"Error extracting listings from page: {e}")
        return listings

    def _extract_page_region(self, soup: BeautifulSoup) -> Optional[str]:
        """
        Pull the parent region name out of the Kleinanzeigen page
        breadcrumb (e.g. "Kleinanzeigen Aachen" -> "Aachen").

        Returns None on state-level or umbrella-category search pages,
        where the breadcrumb is "Kleinanzeigen Immobilien ..." without
        a city name between them.

        Used to enrich the per-card ``Standort`` so it shows the
        parent city as well as the sub-district (e.g.
        "Aachen - Aachen-Mitte" rather than just "Aachen-Mitte").
        """
        try:
            for el in soup.select('[class*="bread"]'):
                txt = el.get_text(" ", strip=True)
                if "Kleinanzeigen" not in txt:
                    continue
                # Two patterns we observe:
                #   Sub-location: "Kleinanzeigen <Region> Immobilien <Cat> in <PLZ> ..."
                #     -> <Region> is the city, between "Kleinanzeigen" and "Immobilien".
                #   State-level:  "Kleinanzeigen Immobilien <Cat> ..."
                #     -> no parent region (return None).
                #
                # We try the sub-location pattern first. If "Immobilien"
                # appears IMMEDIATELY after "Kleinanzeigen" with only
                # whitespace between, that's the umbrella-category
                # breadcrumb — no region.
                m = re.search(
                    r"Kleinanzeigen\s+(?P<region>[A-Za-zÄÖÜäöüß][A-Za-zÄÖÜäöüß\-/]+?)\s+Immobilien\b",
                    txt,
                )
                if m:
                    region = m.group("region").strip()
                    if (region
                        and len(region) >= 2
                        and region.lower() not in {"kleinanzeigen", "startseite",
                                                   "de", "deutschland",
                                                   "immobilien"}):
                        return region
                # Otherwise: state-level / umbrella-category / category-
                # specific page (no city context). Stay None so we
                # don't prefix the card location with "Immobilien".
                return None
        except Exception:
            return None
        return None

    def _has_next_page(self, html: str, current_page: int) -> bool:
        """
        Detect whether a 'next page' link exists.

        Looks for the modern `.pagination` block (with links of the form
        `/s-immobilien/<region>/seite:N/<cat>l<id>`) and for the legacy
        `?o=N` query string.
        """
        try:
            soup = BeautifulSoup(html, get_parser())

            # Modern pagination block
            pagination = soup.select_one(".pagination")
            if pagination:
                # Page numbers or a "next" arrow with a higher page number
                page_links = pagination.select("a[href*='seite:']")
                for link in page_links:
                    href = link.get("href", "")
                    m = re.search(r"/seite:(\d+)/", href)
                    if m and int(m.group(1)) > current_page:
                        return True
                # Next-arrow (› or "Weiter")
                for link in pagination.select("a"):
                    txt = link.get_text(strip=True) or ""
                    href = link.get("href", "") or ""
                    m = re.search(r"/seite:(\d+)/", href)
                    if m and int(m.group(1)) > current_page:
                        return True
                    if "seite:" in href and txt in (">", "›", "»", "Weiter", "Nächste", "Next"):
                        return True

            # Legacy ?o= style
            for link in soup.select("a[href*='o=']"):
                href = link.get("href", "") or ""
                m = re.search(r"[?&]o=(\d+)", href)
                if m and int(m.group(1)) > current_page:
                    return True

            return False
        except Exception as e:
            logger.error(f"Error checking for next page: {e}")
            return False

    # ------------------------------------------------------------------
    # Date fetching
    # ------------------------------------------------------------------
    def _fetch_listing_dates(self, listings: List[Listing]) -> List[Listing]:
        """
        Fetch activation dates for every listing that doesn't have one yet.

        Respects `MAX_LISTINGS_FOR_DATES` if set (None = no cap). Logs
        progress every 10 listings.
        """
        max_listings = self.settings.MAX_LISTINGS_FOR_DATES
        updated: List[Listing] = []
        processed = 0
        skipped = 0
        errors = 0

        for i, listing in enumerate(listings):
            if max_listings is not None and processed >= max_listings:
                logger.warning(
                    f"Reached MAX_LISTINGS_FOR_DATES cap of {max_listings}; "
                    f"{len(listings) - i} listings will not have dates fetched"
                )
                # Append the rest as-is so they're still in the export
                updated.extend(listings[i:])
                break

            if listing.date_parsed:
                updated.append(listing)
                skipped += 1
                continue

            date_str = fetch_listing_date(listing.url, self.session)
            processed += 1

            if date_str:
                date_parsed = parse_kleinanzeigen_date(date_str)
                # IMPORTANT: when reconstructing the Listing from a
                # detail-page fetch, preserve every other field too
                # (postleitzahl, seller_name, price, price_eur, location).
                # Otherwise Excel rows end up with empty columns even
                # though the search card had the data. Using
                # dataclasses.replace avoids having to re-list every
                # field by hand and protects future fields too.
                updated.append(dataclasses.replace(
                    listing,
                    date_posted=date_str,
                    date_parsed=date_parsed,
                ))
            else:
                # No date found; keep the listing but mark as not-old.
                updated.append(listing)
                errors += 1

            if processed % 10 == 0:
                logger.info(
                    f"Date fetch progress: {processed}/{len(listings)} "
                    f"(skipped={skipped}, errors={errors})"
                )

        logger.info(
            f"Date fetch complete: {processed} fetched, {skipped} already had "
            f"dates, {errors} returned no date"
        )
        return updated

    # ------------------------------------------------------------------
    # Seller-name fetching (optional; needed for the Excel "Name of
    # the seller" column).
    # ------------------------------------------------------------------
    def _fetch_seller_names(self, listings: List[Listing]) -> List[Listing]:
        """
        Fetch the seller display name for every listing that doesn't have
        one yet, by hitting each detail page and parsing the user-profile
        section.

        This is a separate phase from ``_fetch_listing_dates`` because:

          * It needs an extra HTTP request per listing (no name is on the
            search card — only on the detail page). On a comprehensive
            NRW run with ~1100+ old listings this adds ~30-60 min.
          * Listing has already been de-duplicated before this point,
            so we only visit each unique URL once.

        Respects ``MAX_LISTINGS_FOR_DATES`` if set (None = no cap) for
        consistency with ``_fetch_listing_dates``.
        """
        max_listings = self.settings.MAX_LISTINGS_FOR_DATES
        updated: List[Listing] = []
        processed = 0
        skipped = 0
        errors = 0

        for i, listing in enumerate(listings):
            if max_listings is not None and processed >= max_listings:
                logger.warning(
                    f"Reached MAX_LISTINGS_FOR_DATES cap of {max_listings}; "
                    f"{len(listings) - i} listings will not have seller names fetched"
                )
                updated.extend(listings[i:])
                break

            if listing.seller_name:
                updated.append(listing)
                skipped += 1
                continue

            seller_name = fetch_listing_seller_name(listing.url, self.session)
            processed += 1

            # Use dataclasses.replace so every other field (price,
            # price_eur, postleitzahl, dates) survives the detail-page
            # fetch. Hand-rolling a Listing here is a data-loss bug
            # waiting to happen every time a new field is added.
            updated.append(dataclasses.replace(
                listing,
                seller_name=seller_name or "",
            ))

            if not seller_name:
                errors += 1

            if processed % 10 == 0:
                logger.info(
                    f"Seller-name fetch progress: {processed}/{len(listings) - skipped} "
                    f"(skipped={skipped}, errors={errors})"
                )

        logger.info(
            f"Seller-name fetch complete: {processed} fetched, "
            f"{skipped} already had names, {errors} returned no name"
        )
        return updated

    # ------------------------------------------------------------------
    # Top-level scraping
    # ------------------------------------------------------------------
    def scrape_bundesland(self, bundesland: str,
                          bundesland_url_param: str,
                          location_id: Optional[str] = None,
                          walk_sub_locations: bool = True,
                          console: Optional["_ui.Console"] = None
                          ) -> ScrapeResult:
        """
        Scrape all real estate listings for a Bundesland.

        Strategy:
          1. Walk the state-level search (locationId = state id) for every
             configured real-estate subcategory.
          2. ALSO walk every sub-location (city) inside the state. This is
             necessary because:
               - The state-level search shows ~3,000 private Häuser zum
                 Kauf listings in NRW, but the site only lets us paginate
                 ~3 pages deep before returning the same cards over and over.
               - The same listings ARE findable via the much smaller
                 per-city searches (each city has 0-100 listings, paginates
                 completely in 1-5 pages).
          3. De-duplicate by listing URL.

        Args:
            bundesland: Display name of the Bundesland (e.g. 'Bayern').
            bundesland_url_param: URL slug (e.g. 'bayern').
            location_id: Numeric Kleinanzeigen locationId from
                bundesland_mapping.json. REQUIRED for the region filter
                to work — without it, the search returns nationwide
                results. Falls back to nationwide if None.
            walk_sub_locations: If True, also walk every city-level search.
                Set to False for a quick state-only scan.

        Returns:
            ScrapeResult object.
        """
        result = ScrapeResult(
            bundesland=bundesland,
            start_time=datetime.now(),
        )

        # Resolve the console (None means no UI). All UI helpers are
        # no-ops when console.enabled is False, so the code below can
        # call them unconditionally.
        console = console or _ui.console
        if console.enabled:
            console.header(f"Scraping {bundesland}")

        if location_id:
            logger.info(f"Starting scrape for {bundesland} (region-filtered via locationId={location_id})")
        else:
            logger.warning(
                f"Starting scrape for {bundesland} WITHOUT locationId — "
                f"results will be nationwide, not region-filtered!"
            )

        # ----- Phase 1: state-level search -----
        category_urls = generate_all_category_urls(bundesland_url_param, location_id)
        logger.info(f"Phase 1: state-level search, {len(category_urls)} subcategories")
        phase1 = console.progress(len(category_urls), label="Phase 1/4 state search")
        for cat_url in category_urls:
            slug = cat_url.split("/s-immobilien/")[-1].split("?")[0]
            logger.info(f"--- Category: {slug} ---")
            self._walk_paginated(cat_url, result)
            phase1.update(suffix=slug)
        phase1.finish()

        # ----- Phase 2: sub-location walker -----
        if walk_sub_locations and location_id:
            sub_locs = fetch_sub_locations(bundesland_url_param, location_id,
                                          session=self.session)
            # Limit breadth to avoid runaway runtime for large states.
            if len(sub_locs) > self.settings.SUB_LOC_BREADTH_LIMIT:
                logger.warning(
                    f"Bundesland has {len(sub_locs)} sub-locations; "
                    f"capping at SUB_LOC_BREADTH_LIMIT={self.settings.SUB_LOC_BREADTH_LIMIT}"
                )
                sub_locs = sub_locs[:self.settings.SUB_LOC_BREADTH_LIMIT]
            logger.info(f"Phase 2: walking {len(sub_locs)} sub-locations inside {bundesland}")
            phase2 = console.progress(len(sub_locs) if sub_locs else 1,
                                      label="Phase 2/4 sub-locations")
            for sub in sub_locs:
                logger.info(f"--- Sub-location: {sub['name']} (id={sub['id']}) ---")
                for cat in Settings.REAL_ESTATE_SUBCATEGORIES:
                    sub_url = build_subcategory_url(cat["code"], sub["id"], page=1)
                    self._walk_paginated(sub_url, result,
                                         max_pages=self.settings.SUB_LOC_MAX_PAGES_PER_CATEGORY)
                phase2.update(suffix=sub["name"])
            phase2.finish()

        # ----- Phase 3: fetch activation dates -----
        logger.info(f"Fetching activation dates for {len(result.listings)} listings...")
        phase3 = console.progress(len(result.listings) if result.listings else 1,
                                  label="Phase 3/4 dates")
        result.listings = self._fetch_listing_dates(result.listings)
        phase3.finish(suffix=f"{len(result.listings)} listings")

        # ----- Phase 4: fetch seller display names -----
        logger.info(f"Fetching seller names for {len(result.listings)} listings...")
        phase4 = console.progress(len(result.listings) if result.listings else 1,
                                  label="Phase 4/4 sellers")
        result.listings = self._fetch_seller_names(result.listings)
        phase4.finish(suffix=f"{len(result.listings)} listings")

        if console.enabled:
            console.ok(f"Scrape finished for {bundesland}")

        # De-duplicate by URL (sub-location walker can re-find listings
        # already seen at state level).
        seen_urls = set()
        unique = []
        for listing in result.listings:
            key = listing.url
            if key in seen_urls:
                continue
            seen_urls.add(key)
            unique.append(listing)
        if len(unique) < len(result.listings):
            logger.info(f"De-duplicated {len(result.listings)} -> {len(unique)} unique listings")
            result.listings = unique
            result.total_listings_found = len(unique)

        # ----- Phase 4: filter and finalize -----
        old_listings = result.get_old_listings()
        result.old_listings_found = len(old_listings)
        result.end_time = datetime.now()

        logger.info(
            f"Done: {result.total_listings_found} unique listings, "
            f"{result.old_listings_found} older than {Settings.MIN_AGE_DAYS} days, "
            f"{result.pages_scraped} pages in {result.duration_seconds:.1f}s"
        )
        return result

    def _walk_paginated(self, base_url: str, result: ScrapeResult,
                        max_pages: Optional[int] = None) -> None:
        """
        Walk every page of a single category URL, appending each batch of
        listings to the shared ScrapeResult. Stops when the site stops
        offering a 'next' link or max_pages is reached.
        """
        page = 1
        page_cap = max_pages if max_pages is not None else self.settings.MAX_PAGES
        while page <= page_cap:
            url = build_page_url(base_url, page)
            logger.info(f"Page {page}: {url}")
            listings = self._fetch_page_with_retry(url)
            if not listings:
                # Either a hard failure or all retries returned empty
                # cards (Kleinanzeigen anti-bot). Skip this page rather
                # than emit 27 empty listings into the export.
                result.errors.append(f"Failed to fetch page {page} of {base_url}")
                page += 1
                continue

            result.listings.extend(listings)
            result.total_listings_found += len(listings)
            result.pages_scraped += 1

            logger.info(f"Found {len(listings)} listings on page {page}")

            if not self._has_next_page(self._last_html_for_pagination, page):
                return
            page += 1

    # Per-instance cache for the last fetched HTML (consumed by
    # `_has_next_page` via `_walk_paginated`). Initialized in __init__
    # below.
    _last_html_for_pagination: str = ""

    def _fetch_page_with_retry(self, url: str,
                               max_attempts: int = 2) -> List:
        """
        Fetch a category-search page and parse its listings, with retry
        for the case where Kleinanzeigen returns a rate-limited /
        anti-bot page that contains zero populated ``.aditem`` cards.

        Returns the parsed listings on success. Returns an empty list
        when every attempt either failed at HTTP level or returned a
        page without populated cards.
        """
        import time
        for attempt in range(1, max_attempts + 1):
            html = self._make_request(url)
            if html is None:
                continue
            # Cache for _has_next_page
            self._last_html_for_pagination = html

            listings = self._extract_listings_from_page(html)
            if not listings:
                # Page returned no cards at all — likely a hard error.
                continue

            # Anti-bot check: if the page returned >= 5 cards but the
            # ratio of populated PLZ cards is below 50%, it's almost
            # certainly an anti-bot / rate-limit page (the real page
            # would have 100% populated). Retry.
            populated = sum(1 for L in listings if L.postleitzahl)
            if populated < len(listings) * 0.5:
                if attempt < max_attempts:
                    # Short backoff
                    wait = 3
                    logger.warning(
                        f"Anti-bot pattern detected on {url} "
                        f"({populated}/{len(listings)} populated). "
                        f"Backing off {wait}s before retry "
                        f"{attempt + 1}/{max_attempts}..."
                    )
                    time.sleep(wait)
                continue

            return listings
        return []

    def close(self):
        """Clean up resources"""
        self.session.close()


def scrape_kleinanzeigen(bundesland: str,
                         bundesland_url_param: str,
                         location_id: Optional[str] = None,
                         walk_sub_locations: bool = True,
                         console: Optional["_ui.Console"] = None
                         ) -> ScrapeResult:
    """
    Convenience function to scrape Kleinanzeigen for a Bundesland.

    Args:
        bundesland: Display name of the Bundesland (e.g. 'Bayern').
        bundesland_url_param: URL slug (e.g. 'bayern').
        location_id: Numeric locationId from bundesland_mapping.json.
        walk_sub_locations: If True (default), also walk every city-level
            search inside the state. Set to False for state-only scan.
        console: Optional UI Console for coloured output + progress
            bar. Pass the module-level ``ui.console`` from main.py when
            the user passes --ui on the CLI. Default: no UI.

    Returns:
        ScrapeResult object.
    """
    scraper = KleinanzeigenScraper()
    try:
        return scraper.scrape_bundesland(bundesland, bundesland_url_param,
                                        location_id,
                                        walk_sub_locations=walk_sub_locations,
                                        console=console)
    finally:
        scraper.close()