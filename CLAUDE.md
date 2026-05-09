# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Commands

```bash
# Install dependencies
pip install -r requirements.txt

# --- Scraper #1: continuous Yad2 + dashboard (config.json driven) ---
python run.py                # Runs server.py + scraper.py together
python scraper.py            # Continuous mode (every N hours per config.json)
python scraper.py --once     # Single scan
python scraper.py --debug    # Test connectivity and parse a sample listing
python scraper.py --reset    # Clear DB and apartments.json
python server.py             # Dashboard only — http://localhost:8080

# --- Scraper #2: one-shot multi-source HTML report (YAML driven) ---
python my_scarper.py                              # Uses ./search_config.yaml
python my_scarper.py --config search_config_2.yaml
```

No test suite exists.

## Architecture

The repo contains **two independent scrapers** that share an HTTP fallback strategy but otherwise have separate configs, outputs, and lifecycles. Don't conflate them.

### Scraper #1 — `scraper.py` + `server.py` + `run.py` (continuous Yad2 → SQLite → dashboard)

Scrapes Yad2 only; runs continuously; persists state in SQLite; serves a live dashboard. Current target city is **Rehovot** (`city_code: 8400`, slug `center-and-sharon`); changing target city requires updating both `config.json` (`search.city_code`) and the slug in `build_feed_url` (scraper.py:174,185).

Data flow:
1. Extracts Yad2's dynamic Next.js build ID from the main rent page HTML on each cycle (changes on every Yad2 deploy).
2. Uses that build ID to construct `_next/data/{BUILD_ID}/rent/center-and-sharon.json` API URLs.
3. Iterates over neighborhood groups (by Yad2 neighborhood IDs) plus a broad city-wide query.
4. Parses/filters listings and upserts into `apartments.db` (SQLite, WAL mode).
5. Exports `apartments.json` for the dashboard, sorted by `is_new DESC, parking, price ASC`.

`server.py` is a minimal `http.server.SimpleHTTPRequestHandler` that serves `dashboard.html` at `/`, `apartments.json` at `/api/apartments`, and `config.json` at `/api/config`. `run.py` simply spawns `server.py` and `scraper.py` as subprocesses and waits on the scraper.

**Configuration & neighborhood filtering.** `config.json` is reloaded at the start of every scan cycle — no restart needed. Two filtering layers stack:

1. **Neighborhood ID filter** (server-side): the inline `neighborhood_groups` list inside `scrape()` (scraper.py:467) is the runtime source of truth for which Yad2 neighborhood IDs to query. The top-level `NEIGHBORHOODS` dict (scraper.py:74) is currently unreferenced documentation — edit `neighborhood_groups` to change which areas are queried.
2. **Address-text keyword filter** (client-side): `target_areas.hebrew` + `target_areas.english` substrings are matched against `address + neighborhood + street` text. Listings from the broad query are kept only if they match a keyword.

`--debug` mode hard-codes TA-area neighborhood IDs `[1483, 1484]` for its single test fetch (scraper.py:621); update those if debugging against a different city.

### Scraper #2 — `my_scarper.py` (one-shot, multi-source, HTML report + optional email)

Despite the typo in the filename, this is a fully-featured scraper, not an experiment. It fetches from **Madlan** (via `scrapling`'s `StealthyFetcher` — headless browser to extract the SSR `__SSR_HYDRATED_CONTEXT__` blob) and **Yad2** (same Next.js build-ID trick as scraper.py), normalizes both into a common shape, filters by neighborhood substring, writes `reports/rental_<town>_<YYYY-MM-DD>.html`, and optionally emails the report via SMTP.

It is **not** invoked by `run.py` and does **not** read `config.json` or write to `apartments.db`. Its config is YAML (`search_config.yaml` by default; `search_config_2.yaml` is a second saved query). Schema: `town`, `neighborhoods` (substring filter), `sources` (`madlan`, `yad2`, or both), `filters` (price/rooms), `email`, `smtp`.

Adding a town requires editing the `TOWNS` dict at `my_scarper.py:45`, which maps a town key to `display`, `madlan_slug` (Hebrew place token in Madlan's `/for-rent/<slug>` URL), `yad2_city_code`, and `yad2_slug`. A YAML `town:` value must match a key in this dict.

### Shared: HTTP library fallback

Both scrapers prefer `curl_cffi` (with `impersonate="chrome124"` for Cloudflare bypass) and fall back to `requests`. `scraper.py` additionally falls back to `cloudscraper`. Each fetch retries up to 3 times with random backoff.

### Generated files (not in git)

`apartments.db`, `apartments.json`, `scraper.log` (scraper #1); `reports/` (scraper #2).
