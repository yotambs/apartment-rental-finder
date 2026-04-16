# Finder Bot v2.0

Automated scraper + dashboard for finding rental apartments.

## Quick Start

```bash
pip install curl_cffi cloudscraper requests

# Continuous mode:
python run.py # (the bot will scan all relevant apratments based on the configuration in the config.json file.

# Dashboard:
# Once you see on the screen "Next in 3h... " open your dashboard using: http://localhost:8080

# to reset the db:

python scraper.py --reset

```

## What's Configurable (config.json)

Edit `config.json` to control everything — no code changes needed:

| Section | What you control |
|---------|-----------------|
| `search.rooms_min/max` | Room range (default 2–3) |
| `search.price_min/max` | Price range in ₪ (default 7000–10000) |
| `search.exclude_ground_floor` | Skip ground floor (default true) |
| `target_areas.hebrew` | Hebrew neighborhood/street keywords |
| `target_areas.english` | English area keywords |
| `schedule.interval_hours` | Hours between scans (default 3) |
| `schedule.delay_between_requests_sec` | Politeness delay (default 2.5s) |
| `notifications.telegram_*` | Telegram bot alerts |
| `notifications.email_*` | Email alerts via SMTP |
| `dashboard_port` | Web server port (default 8080) |

Config is reloaded every cycle — edit it live without restarting.

## How It Works

2. **Cloudflare bypass** — Uses `cloudscraper` with rotating User-Agents
3. **Area filtering** — Matches listings against Hebrew/English neighborhood keywords (צפון הישן, רוטשילד, הבימה, רידינג, etc.)
4. **New detection** — SQLite tracks every listing ID; new ones get flagged and highlighted
5. **Sorting** — New listings first → with parking → lower price
6. **Notifications** — Optional Telegram and/or email alerts for new finds

## Files

```
config.json       ← Edit this to control everything
scraper.py        ← Main bot (run this)
server.py         ← Dashboard web server
dashboard.html    ← Frontend UI
requirements.txt  ← Python dependencies
apartments.db     ← SQLite database (auto-created)
apartments.json   ← Dashboard data (auto-created)
scraper.log       ← Timestamped log
```

## Telegram Setup

1. Message @BotFather on Telegram → `/newbot` → get token
2. Message @userinfobot → get your chat ID
3. Edit config.json:
```json
"telegram_enabled": true,
"telegram_bot_token": "123456:ABC...",
"telegram_chat_id": "987654321"
```


