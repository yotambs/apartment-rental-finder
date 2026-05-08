# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Commands

```bash
# Install dependencies
pip install -r requirements.txt

# Run scraper + dashboard together
python run.py

# Scraper only
python scraper.py            # Continuous mode (every N hours per config)
python scraper.py --once     # Single scan
python scraper.py --debug    # Test connectivity and parse a sample listing
python scraper.py --reset    # Clear DB and apartments.json

# Dashboard only
python server.py             # http://localhost:8080
```

No test suite exists.

## Architecture

The project scrapes rental listings from Yad2 (Israeli real estate site) and displays them on a local web dashboard. The current target city is **Rehovot** (`city_code: 8400`, slug `center-and-sharon`); changing target city requires updating both `config.json` (`search.city_code`) and the slug in `build_feed_url` (scraper.py:174,185).

**Data flow:**
1. `scraper.py` extracts Yad2's dynamic Next.js build ID from the main rent page HTML on each cycle (it changes on every Yad2 deploy).
2. Uses that build ID to construct `_next/data/{BUILD_ID}/rent/center-and-sharon.json` API URLs.
3. Iterates over neighborhood groups (by Yad2 neighborhood IDs) plus a broad city-wide query.
4. Parses/filters listings and upserts into `apartments.db` (SQLite, WAL mode).
5. Exports `apartments.json` for the dashboard, sorted by `is_new DESC, parking, price ASC`.

**HTTP library fallback chain** (in order of preference): `curl_cffi` → `cloudscraper` → `requests`. The `curl_cffi` library with `impersonate="chrome124"` is the primary Cloudflare bypass mechanism. Each fetch retries up to 3 times with random backoff.

**`server.py`** is a minimal `http.server.SimpleHTTPRequestHandler` that serves `dashboard.html` at `/`, `apartments.json` at `/api/apartments`, and `config.json` at `/api/config`.

**`run.py`** simply spawns `server.py` and `scraper.py` as subprocesses and waits on the scraper.

**`my_scarper.py`** (sic) is a standalone experimental scraper for `madlan.co.il` using the `scrapling` library — not invoked by `run.py` or `scraper.py`, kept as a parallel exploration.

### Configuration & neighborhood filtering

`config.json` is reloaded at the start of every scan cycle — no restart needed. Two filtering layers stack:

1. **Neighborhood ID filter** (server-side): the inline `neighborhood_groups` list inside `scrape()` (scraper.py:467) is the runtime source of truth for which Yad2 neighborhood IDs to query. The top-level `NEIGHBORHOODS` dict (scraper.py:74) is currently unreferenced documentation — edit `neighborhood_groups` to change which areas are queried.
2. **Address-text keyword filter** (client-side): `target_areas.hebrew` + `target_areas.english` substrings are matched against `address + neighborhood + street` text. Listings from the broad query are kept only if they match a keyword.

`--debug` mode hard-codes TA-area neighborhood IDs `[1483, 1484]` for its single test fetch (scraper.py:621); update those if debugging against a different city.

**Generated files** (not in git): `apartments.db`, `apartments.json`, `scraper.log`.
