"""
Settings and configuration for Kleinanzeigen.de Real Estate Scraper
"""

import os
from pathlib import Path
from typing import Tuple, List


class Settings:
    """Application settings"""

    # Base URLs
    BASE_URL = "https://www.kleinanzeigen.de"

    # The umbrella "Immobilien" category on modern Kleinanzeigen.
    # Sub-categories use the bare numeric code (e.g. c208 "Häuser zum Kauf").
    IMMOBILIEN_CATEGORY = "c195"

    # Query parameters always appended to every search URL.
    # - posterType=PRIVATE: only show listings from private sellers. Without
    #   this the default search surfaces commercial listings (e.g. real-estate
    #   agencies that post hundreds of new listings daily), which bury the
    #   private listings that have been sitting around.
    # - sortingField=SORTING_DATE: newest activation first. The page-counter
    #   advances until no more pages are returned; combined with the
    #   >90-day filter, this surfaces old private listings.
    DEFAULT_QUERY_PARAMS = "posterType=PRIVATE&sortingField=SORTING_DATE"

    # Request configuration
    # (min_seconds, max_seconds) delay between requests. ~2s is a reasonable
    # compromise between politeness and runtime.
    REQUEST_DELAY: Tuple[int, int] = (2, 4)
    MAX_PAGES = 25            # Safety limit per category (was 50)
    REQUEST_TIMEOUT = 30      # Request timeout in seconds
    MAX_RETRIES = 3           # Maximum retries for failed requests

    # Date fetching configuration
    # Limits how many listings per run we follow up with a detail-page fetch
    # to grab the activation date. Default is None (no cap). Set to a small
    # int ONLY for smoke-tests.
    MAX_LISTINGS_FOR_DATES: int | None = None

    # Sub-location walker settings
    # When walking every city inside a Bundesland, we keep these limits to
    # avoid hammering the server and to bound runtime:
    #   - Each sub-location/category gets at most this many pages.
    #   - Stop walking that sub-location as soon as we see a page with zero
    #     private listings (the sub-location probably has none in that cat).
    SUB_LOC_MAX_PAGES_PER_CATEGORY = 3
    SUB_LOC_BREADTH_LIMIT = 100     # max sub-locations to walk per run

    # User agents for rotation
    USER_AGENTS = [
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:121.0) Gecko/20100101 Firefox/121.0",
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.1 Safari/605.1.15",
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Edge/120.0.0.0 Safari/537.36",
    ]

    # File paths
    PROJECT_ROOT = Path(__file__).parent.parent
    DATA_DIR = PROJECT_ROOT / "data"
    OUTPUT_DIR = DATA_DIR / "output"
    BUNDESLAND_MAPPING_FILE = DATA_DIR / "bundesland_mapping.json"

    # Logging
    LOG_LEVEL = "INFO"
    LOG_FORMAT = "%(asctime)s - %(name)s - %(levelname)s - %(message)s"
    LOG_FILE = PROJECT_ROOT / "scraper.log"

    # Date filtering
    MIN_AGE_DAYS = 90  # Listings older than this will be included

    # Price filtering
    # Keep only listings whose lowest numeric price is strictly above
    # this floor (EUR). VB or no VB, anything at or below is dropped.
    # Effective since 2026-07-03 per user request — the prior rule
    # ("VB + < 1000 €" or missing) let cheap fixtures and €1 placeholders
    # into the export, which the user did not want.
    # Effective 2026-07-03: lowered from 150 000 to 100 000 per user
    # request ("Filter to only collect listings that have price > 100000").
    # NOTE: this floor still effectively empties c203 (Mietwohnung)
    # because apartment rents are monthly and never reach 100 000 €
    # on the listing side. If rent listings are wanted, lower the
    # floor further or split the rule per category.
    MIN_PRICE_EUR = 100000

    # Excel output
    EXCEL_FILENAME_TEMPLATE = "{bundesland}_real_estate_old_listings_{timestamp}.xlsx"
    EXCEL_DATE_FORMAT = "%Y-%m-%d_%H-%M-%S"

    # Global xlsx — single fixed filename that accumulates listings
    # from every Bundesland run. Existing rows (matched by URL) are
    # skipped on re-runs so the file only ever grows with newly
    # discovered listings. See ExcelExporter.export_global() for the
    # dedup implementation.
    GLOBAL_FILENAME = "Global_real_estate_old_listings.xlsx"
    GLOBAL_SHEET_NAME = "Global Old Listings"

    # Headers for HTTP requests
    DEFAULT_HEADERS = {
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/webp,*/*;q=0.8",
        "Accept-Language": "de-DE,de;q=0.8,en-US;q=0.5,en;q=0.3",
        "Accept-Encoding": "gzip, deflate, br",
        "Connection": "keep-alive",
        "DNT": "1",
        "Upgrade-Insecure-Requests": "1",
    }

    # Real estate subcategories to scrape.
    # Each entry uses the modern numeric category code on the URL path:
    #   /s-immobilien/<region>/c<code>l<locationId>?<query>
    # Slugs were tried but they only do free-text matching, not real category
    # filtering. The numeric codes below were verified against live
    # Kleinanzeigen.de on 2026-06-28.
    #
    # Curated set per user request: keep only the five categories that are
    # actually useful for surfacing stale private-seller listings.
    #   c196 Eigentumswohnung kaufen
    #   c198 Weitere Immobilien
    #   c203 Mietwohnung
    #   c207 Grundstücke & Gärten
    #   c208 Häuser zum Kauf (e.g. the user's example listing)
    #
    # Removed (use a full set if you need them again):
    #   c195 Immobilien (umbrella — overlaps with the umbrella search)
    #   c197 Garage & Lagerraum
    #   c199 Auf Zeit & WG
    #   c205 Häuser zur Miete
    REAL_ESTATE_SUBCATEGORIES: List[dict] = [
        {"code": "c196", "label": "Eigentumswohnung kaufen"},
        {"code": "c198", "label": "Weitere Immobilien"},
        {"code": "c203", "label": "Mietwohnung"},
        {"code": "c207", "label": "Grundstücke & Gärten"},
        {"code": "c208", "label": "Häuser zum Kauf"},
    ]

    # Validate and create directories
    @classmethod
    def validate_directories(cls):
        """Ensure required directories exist"""
        cls.OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
        cls.DATA_DIR.mkdir(parents=True, exist_ok=True)


# Initialize settings
Settings.validate_directories()

# Create a settings instance for easy access
settings = Settings()