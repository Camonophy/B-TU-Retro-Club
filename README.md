# Kleinanzeigen.de Real Estate Scraper

A Python application that scrapes Kleinanzeigen.de for real estate listings in a German Bundesland, filters for listings that haven't been bumped in more than 3 months (90 days), and exports the results to Excel.

## Prerequisites

### Linux (Ubuntu/Debian)

Before installing Python dependencies, you need to install system libraries for `lxml`:

```bash
sudo apt-get update
sudo apt-get install -y libxml2-dev libxslt1-dev python3-dev zlib1g-dev
```

### Windows

On Windows, `lxml` can be installed directly via pip without additional system dependencies:

```cmd
pip install lxml
```

If you encounter issues, try using pre-built wheels:

```cmd
pip install --only-binary :all: lxml
```

### macOS

```bash
# Using Homebrew
brew install libxml2 libxslt
brew link libxml2 --force
brew link libxslt --force
```

## Quick Start

```bash
# Create virtual environment (recommended)
python -m venv venv
source venv/bin/activate  # On Linux/macOS
venv\Scripts\activate     # On Windows

# Install dependencies
pip install -r requirements.txt

# List available Bundeslaender
python main.py --list

# Scrape a specific Bundesland
python main.py --bundesland "Bayern"

# Scrape and export ALL listings (not just old ones)
python main.py --bundesland "Bayern" --all

# Run in interactive mode
python main.py --interactive

# Run with the optional terminal UI (ANSI colours + progress bar)
python main.py --bundesland "Bayern" --ui
python main.py --interactive --ui

# Run the test suite
python test_basic.py
```

## Alternative: Install without lxml

If you cannot install `lxml`, you can use Python's built-in HTML parser instead:

```bash
pip install requests beautifulsoup4 pandas openpyxl python-dateutil
```

**Note**: The built-in `html.parser` is slower than `lxml` and may have slightly different parsing behavior, but it will work for most cases.

## What the Scraper Actually Does

When you run `python main.py --bundesland "Nordrhein-Westfalen"`, the scraper:

### Phase 1: Initialization

1. **Load Configuration**: Reads settings from `config/settings.py` and the Bundesland → locationId mapping from `data/bundesland_mapping.json`.
2. **Validate Bundesland**: Looks up the requested Bundesland's `url_param` and `location_id` (a numeric ID that Kleinanzeigen uses internally to scope searches to a state). Without `location_id` the search returns nationwide results.
3. **Setup Logging**: Initializes logging to console and `scraper.log`.
4. **Create Session**: Sets up an HTTP session with a rotating User-Agent.

### Phase 2: Build Search URLs

For each configured subcategory (`mietwohnung`, `wohnung`, `haus`, … — 11 in total), the scraper builds a region-filtered URL of the form:

```
https://www.kleinanzeigen.de/s-immobilien/<region>/<subcategory>/k0c195l<locationId>
```

The `l<locationId>` suffix is what tells Kleinanzeigen to scope the search to the Bundesland; without it the site ignores the region slug in the path and returns nationwide results.

### Phase 3: Paginate and Scrape Search Results

For each subcategory the scraper walks pages until either it has checked all 25 pages (the safety limit) or the site stops offering a "next" link. From each result card it extracts:

- **Title** (`<h2 class="text-module-begin"><a>`)
- **URL** (from the title's `href` or the `data-href` attribute on the article)
- **Price** (`<p class="aditem-main--middle--price-shipping--price">`)
- **Location** (`<div class="aditem-main--top--left">`)

The **activation date is NOT extracted from the search results** — Kleinanzeigen loads it via JavaScript. The scraper fetches each listing's detail page in Phase 4.

### Phase 4: Fetch Activation Dates

For each listing, the scraper visits the detail page and extracts the activation date from the calendar icon block:

```
<div id="viewad-extra-info" class="boxedarticle--details--full">
  <div>
    <i class="icon icon-small icon-calendar-gray-simple"></i>
    <span>24.06.2026</span>
  </div>
</div>
```

This date is the **last activation date** of the listing (i.e. when the seller last bumped it). Listings whose activation date is older than 90 days are exactly the "neglected" ones a buyer looking for stale inventory wants to find, so this IS the right signal.

The scraper deliberately ignores the seller's "Aktiv seit" date in the user profile (which is the seller's account age, not the listing's).

### Phase 5: Filter and Export

- Filter listings where `age_days > 90`.
- Apply one additional content filter to every search card before it is added to the dataset (see [scraper/utils.py](scraper/utils.py)):
  - **For-sale only.** Titles matching wanted-listing signals (`Suche`, `Gesucht`, `Bewerber`, `Tausch`, …) are dropped. Titles matching sell signals (`zu verkaufen`, `zu vermieten`, `biete`, …) are kept. Ambiguous titles are kept by default.
  - **Price floor.** Listings whose lowest numeric price is ≤ `Settings.MIN_PRICE_EUR` (100 000 €) are dropped, regardless of whether the price cell contains `VB` or not. Two-price ranges like `"100.001 € VB 200.000 €"` use the lower bound; here the lower bound is above the floor so the listing is kept. `"80.000 € VB 180.000 €"` is dropped because the lower bound (80 000) is at or below the floor. Listings with no parsable price are also dropped. This rule effectively empties `c203` (Mietwohnung) because apartment rents are monthly and never reach 100 000 € on the listing side — if rent listings are wanted, lower the floor or split the rule per category.
- Export to the single fixed `data/output/Global_real_estate_old_listings.xlsx` (sheet `Global Old Listings`, truncated to Excel's 31-char limit) plus a `Summary` sheet. The global file accumulates listings across every run and every Bundesland — listings already present (matched by URL) are skipped, only newly discovered URLs are appended. The `Aktueller Wert (€)` column contains a comma-separated integer (e.g. `25,000`) — no `€`, no `VB`; rows with no parsable price have an empty cell. When the search-card price cell shows TWO numbers (e.g. `"215.000 € VB 265.000 €"` or `"550 € 700 €"`) the higher one is written into a separate last column called `Vorheriger Wert (€)`; single-price cells leave that column empty (no repeated value). Rows are written sorted by activation date ASCENDING (= oldest-online listing first = largest age first = "descending by time since online" in the user's words) — sort happens client-side because Kleinanzeigen's date sort returns newest-first, opposite of what we want. Empty / unparseable activation date sorts to the end so dated rows stay grouped at the top.

If no listings are found at all (scraper returned 0 results), the file is still written with just the header + Summary sheet so subsequent runs have a baseline to dedup against.

## Features

- Region-filtered search across all 16 German Bundesländer
- 11 real estate subcategories (Mietwohnung, Wohnung, Haus, Eigentumswohnung, etc.)
- Modern URL scheme with `locationId` so the search is actually scoped to the requested state
- Activation-date extraction from listing detail pages (calendar icon in `viewad-extra-info`)
- Proper handling of German relative date strings ("Heute", "vor 2 Monaten", etc.) and absolute dates ("24.06.2026")
- 7-column Excel export with Postleitzahl, Seller, Standort, Aktueller Wert (€), Datum seit online, Link, Vorheriger Wert (€) (the last column is populated only when the search-card price cell shows two prices)
- Summary sheet with per-run statistics
- Random delay between requests (default 2-4 s) and rotating User-Agent
- Fallback from `lxml` to Python's built-in `html.parser` if lxml is unavailable
- Graceful error handling with per-page error collection
- CLI mode and interactive mode
- Test suite that actually reports failures (`python test_basic.py`)

## Project Structure

```
ImmoRetterApp/
├── main.py                          # Main entry point (CLI + interactive)
├── README.md                        # This file
├── requirements.txt                 # Dependencies
├── test_basic.py                    # Test suite (real pass/fail reporting)
├── config/
│   └── settings.py                  # Configuration
├── scraper/
│   ├── __init__.py
│   ├── models.py                    # Data models (Listing, ScrapeResult)
│   ├── utils.py                     # URL generation + date parsing + date fetching
│   ├── kleinanzeigen.py             # Core scraping logic
│   └── exporter.py                  # Excel export
└── data/
    ├── bundesland_mapping.json      # Bundesland → url_param + location_id
    └── output/                      # Excel files output
```

## Configuration

You can tweak these in `config/settings.py`:

| Setting                   | Default  | Meaning                                                                    |
|---------------------------|----------|----------------------------------------------------------------------------|
| `REQUEST_DELAY`             | `(2, 4)`   | Random delay in seconds between requests                                   |
| `MAX_PAGES`                 | `25`       | Max pages scraped per subcategory (safety limit)                           |
| `REQUEST_TIMEOUT`           | `30`       | HTTP timeout in seconds                                                    |
| `MAX_RETRIES`               | `3`        | HTTP retries on transient errors                                           |
| `MAX_LISTINGS_FOR_DATES`    | `None`     | Optional cap on how many detail-page date fetches are made (None = no cap) |
| `MIN_AGE_DAYS`              | `90`       | Listings whose activation date is older than this are exported             |
| `MIN_PRICE_EUR`             | `100000`   | Listings whose lowest numeric price is at or below this floor are dropped |
| `REAL_ESTATE_SUBCATEGORIES` | 5 codes    | Which subcategories to scrape (c196, c198, c203, c207, c208)              |

### Tuning for Faster Runs

A full scrape of one Bundesland with default settings can take 30-60+ minutes (25 pages × 11 categories × ~3s delay + 1 detail-page fetch per listing × ~1s each). For quick experimentation, edit `config/settings.py`:

```python
MAX_PAGES = 3                       # only 3 pages per subcategory
MAX_LISTINGS_FOR_DATES = 50         # cap detail-page date fetches at 50
REQUEST_DELAY = (0.3, 0.6)          # aggressive delay
```

With these, a run against Bremen completes in about 90 seconds.

## Troubleshooting

### "Couldn't find a tree builder with the features you requested: lxml"

Either install lxml (see prerequisites above), or let the scraper fall back to `html.parser`:

```bash
pip install requests beautifulsoup4 pandas openpyxl python-dateutil
```

The fallback is automatic; no code change needed.

### Rate Limited / Blocked

- Increase `REQUEST_DELAY` in `config/settings.py`
- Use a VPN or proxy
- Try again later

### No Old Listings Found

- Try a different (larger) Bundesland — smaller ones like Bremen have very few stale listings.
- Re-run with `--all` to inspect what came back.
- Verify the search is region-filtered: check that the title of the search page includes "in <Bundesland>".

### Tests Fail

The test suite in `test_basic.py` now actually reports failures. If you see ❌ markers, the corresponding functionality is broken — usually because a dependency is missing or `data/bundesland_mapping.json` is malformed.

## Disclaimer

This tool is for educational and personal use only. Web scraping may violate Kleinanzeigen.de's terms of service. Use responsibly and respect their servers.