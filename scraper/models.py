"""
Data models for Kleinanzeigen Scraper
"""

from dataclasses import dataclass, field
from datetime import datetime
from typing import Optional, List


@dataclass
class Listing:
    """Represents a single Kleinanzeigen listing"""

    title: str
    url: str
    price: Optional[str] = None
    # Numeric price in EUR (lowest value when the listing shows a range
    # or a "VB" pair). None when the price is missing or unparseable.
    # Set by _parse_listing_card via parse_price_eur(); the exporter
    # uses this to write the "Aktueller Wert (€)" column as a pure
    # number instead of the raw "215.000 € VB 265.000 €" string.
    price_eur: Optional[float] = None
    # Numeric "previous price" in EUR: the higher of two values when
    # the search-card price cell shows a pair like "215.000 € VB
    # 265.000 €" or "550 € 700 €". None for single-price cells
    # ("184.500 €", "1 €") — the user explicitly does not want a
    # repeated value in a separate column when there was no previous
    # price. Surfaced as the "Vorheriger Wert (€)" spreadsheet column.
    # Set by _parse_listing_card via parse_price_pair_eur().
    previous_price_eur: Optional[float] = None
    location: Optional[str] = None
    date_posted: Optional[str] = None
    date_parsed: Optional[datetime] = None
    age_days: Optional[int] = None
    is_older_than_3_months: bool = False
    # Postleitzahl (German postal code, 5 digits) extracted from the
    # search-card location text. May be "" if the location text was
    # unusual (e.g. only the city name appeared).
    postleitzahl: str = ""
    # Display name of the seller (Kleinanzeigen user), fetched from the
    # detail page. "" if not yet fetched or no name was found.
    seller_name: str = ""

    def __post_init__(self):
        """Calculate derived fields after initialization"""
        if self.date_parsed:
            self.age_days = self.calculate_age()
            self.is_older_than_3_months = self.age_days > 90 if self.age_days else False

    def calculate_age(self) -> Optional[int]:
        """Calculate age in days from parsed date"""
        if self.date_parsed:
            delta = datetime.now() - self.date_parsed
            return delta.days
        return None

    def to_dict(self) -> dict:
        """Convert to dictionary for Excel export.

        The exporter defines its own column order independently, but
        ``to_dict`` is kept for any external consumer.
        """
        return {
            "Title": self.title,
            "URL": self.url,
            "Price": self.price or "",
            "Location": self.location or "",
            "Date Posted": self.date_posted or "",
            "Age (Days)": self.age_days or "",
            "Older than 3 months": "Yes" if self.is_older_than_3_months else "No",
            "Postleitzahl": self.postleitzahl or "",
            "Seller Name": self.seller_name or "",
        }

    # Note: Listing.to_dict() intentionally does NOT include
    # previous_price_eur — the dict's column set is frozen at the
    # old per-Bundesland exporter's shape. The global-accumulator
    # exporter reads previous_price_eur directly off the Listing via
    # _listing_row(); if a future caller wires to_dict() into the
    # exporter path, add previous_price_eur here too.


@dataclass
class ScrapeResult:
    """Represents the result of a scraping operation"""

    bundesland: str
    total_listings_found: int = 0
    old_listings_found: int = 0
    listings: List[Listing] = field(default_factory=list)
    pages_scraped: int = 0
    errors: List[str] = field(default_factory=list)
    start_time: Optional[datetime] = None
    end_time: Optional[datetime] = None
    # Populated by ExcelExporter.export_global(): how many listings
    # this run added to the global xlsx (new) vs. how many were
    # skipped because their URL was already present (duplicates).
    new_global_rows: int = 0
    duplicate_global_rows: int = 0
    
    @property
    def duration_seconds(self) -> Optional[float]:
        """Calculate duration in seconds"""
        if self.start_time and self.end_time:
            return (self.end_time - self.start_time).total_seconds()
        return None
    
    def get_old_listings(self) -> List[Listing]:
        """Get only listings older than 3 months"""
        return [listing for listing in self.listings if listing.is_older_than_3_months]
    
    def to_summary_dict(self) -> dict:
        """Convert to summary dictionary"""
        return {
            "Bundesland": self.bundesland,
            "Total Listings Found": self.total_listings_found,
            "Old Listings (>3 months)": self.old_listings_found,
            "Pages Scraped": self.pages_scraped,
            "Duration (seconds)": self.duration_seconds,
            "Errors": len(self.errors),
        }
