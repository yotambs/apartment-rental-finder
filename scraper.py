#!/usr/bin/env python3
"""
============================
Uses Yad2's actual Next.js _next/data endpoint (discovered April 2026).

URL pattern:
  https://www.yad2.co.il/realestate/_next/data/{BUILD_ID}/rent/tel-aviv-area.json
  ?minRooms=2&maxRooms=3&multiCity=5000&minPrice=7000&maxPrice=10000&slug=tel-aviv-area

The BUILD_ID changes on each Yad2 deploy — we extract it from the main page.

Usage:
  python scraper.py --debug    # Test connectivity
  python scraper.py --once     # Single scan
  python scraper.py            # Continuous (every N hours)
  python scraper.py --reset    # Clear DB
"""

import json, logging, os, random, re, smtplib, sqlite3, sys, time
from datetime import datetime
from email.mime.text import MIMEText
from pathlib import Path

try:
    from curl_cffi import requests as http
    HTTP_LIB = "curl_cffi"
except ImportError:
    try:
        import cloudscraper
        _cs = cloudscraper.create_scraper(browser={"browser":"chrome","platform":"windows","desktop":True})
        class _CS:
            @staticmethod
            def get(url, **kw):
                kw.pop("impersonate", None)
                return _cs.get(url, **kw)
        http = _CS()
        HTTP_LIB = "cloudscraper"
    except ImportError:
        import requests as http
        HTTP_LIB = "requests"

DIR = Path(__file__).parent
DB = DIR / "apartments.db"
OUT = DIR / "apartments.json"

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[
        logging.FileHandler(DIR / "scraper.log", encoding="utf-8"),
        logging.StreamHandler(sys.stdout),
    ],
)
log = logging.getLogger("yad2")

def load_cfg():
    with open(DIR / "config.json", "r", encoding="utf-8") as f:
        return json.load(f)

# --- Constants ---
YAD2_BASE = "https://www.yad2.co.il"
YAD2_RENT_PAGE = f"{YAD2_BASE}/realestate/rent"
ITEM_URL = f"{YAD2_BASE}/item/{{}}"

# Known Tel Aviv neighborhood IDs (from Yad2's address-master)
#"old_north_north": 1483,  # הצפון הישן - צפון
#     "old_north_south":  1484,  # הצפון הישן - דרום
#     "lev_hair":         1520,  # לב תל אביב / לב העיר
#     "habima":           1489,  # הבימה / לב העיר
#     "new_north":        1485,  # הצפון החדש - כיכר המדינה
# }
#

NEIGHBORHOODS = {
    "NW District": 20060009,  # הצפון הישן - צפון
    "Neve Amit": 1211,  # הצפון הישן - דרום
    "Weizmann Institute": 1236,  # לב תל אביב / לב העיר
    "Achuzat HaNasi": 1235,  # הבימה / לב העיר
    "Chavatzelet": 1216,  # הצפון החדש - כיכר המדינה
}

# │ 20060009 │ ב' / צפון מערב העי│ NW District│
# │ 1211 │ נווה עמית│ Neve Amit│
# 1213 │ נווה יהודה│ Neve Yehuda│
# │ 1236│ מכון ויצמן│ Weizmann Institute│
# │ 1235│ אחוזת הנשיא│ Achuzat HaNasi│
#  │ 1216 │ חבצלת│ Chavatzelet│


UAS = [
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/126.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 14_5) AppleWebKit/605.1.15 Version/17.5 Safari/605.1.15",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:128.0) Gecko/20100101 Firefox/128.0",
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 Chrome/126.0.0.0 Safari/537.36",
]

def hdrs():
    return {
        "User-Agent": random.choice(UAS),
        "Accept": "application/json, text/html, */*",
        "Accept-Language": "he-IL,he;q=0.9,en;q=0.7",
        "Referer": YAD2_RENT_PAGE,
    }

def fetch(url, as_json=True, retries=3):
    """Fetch a URL with TLS impersonation and retries."""
    for attempt in range(retries):
        try:
            kw = {"headers": hdrs(), "timeout": 30}
            if HTTP_LIB == "curl_cffi":
                kw["impersonate"] = "chrome124"
            r = http.get(url, **kw)
            if hasattr(r, "status_code") and r.status_code != 200:
                log.warning(f"HTTP {r.status_code} from {url[:80]}")
                return None
            return r.json() if as_json else r.text
        except Exception as e:
            log.warning(f"Fetch attempt {attempt+1}/{retries} failed: {e}")
            if attempt < retries - 1:
                time.sleep(2 + random.uniform(1, 3))
    return None


# ============================================================================
# BUILD ID EXTRACTION
# ============================================================================

def get_build_id():
    """Extract the Next.js build ID from the main Yad2 page."""
    html = fetch(YAD2_RENT_PAGE, as_json=False)
    if not html:
        log.error("Cannot fetch Yad2 main page")
        return None

    # Pattern 1: _next/data/BUILD_ID/ in script src
    m = re.search(r'/_next/data/([a-zA-Z0-9_-]+)/', html)
    if m:
        bid = m.group(1)
        log.info(f"Build ID (from _next/data): {bid}")
        return bid

    # Pattern 2: __NEXT_DATA__ JSON
    m = re.search(r'"buildId"\s*:\s*"([^"]+)"', html)
    if m:
        bid = m.group(1)
        log.info(f"Build ID (from __NEXT_DATA__): {bid}")
        return bid

    # Pattern 3: any _next/static/BUILD_ID
    m = re.search(r'/_next/static/([a-zA-Z0-9_-]{10,})/', html)
    if m:
        bid = m.group(1)
        log.info(f"Build ID (from _next/static): {bid}")
        return bid

    log.error("Could not find build ID in page")
    return None


# ============================================================================
# FEED FETCHING
# ============================================================================

def build_feed_url(build_id, cfg, page=1, neighborhoods=None):
    """Build the _next/data feed URL."""
    s = cfg["search"]
    params = {
        "minRooms": str(s["rooms_min"]),
        "maxRooms": str(s["rooms_max"]),
        "minPrice": str(s["price_min"]),
        "maxPrice": str(s["price_max"]),
        "multiCity": s["city_code"],
        "slug": "center-and-sharon",
    }
    if s.get("min_sqm"):
        params["squareMeterMin"] = str(s["min_sqm"])
    if s.get("require_elevator"):
        params["elevator"] = "1"
    if neighborhoods:
        params["multiNeighborhood"] = ",".join(str(n) for n in neighborhoods)
    if page > 1:
        params["page"] = str(page)

    qs = "&".join(f"{k}={v}" for k, v in params.items())
    return f"{YAD2_BASE}/realestate/_next/data/{build_id}/rent/center-and-sharon.json?{qs}"


def extract_listings(data):
    """Extract apartment listings from Yad2's Next.js response."""
    items = []
    try:
        queries = data.get("pageProps", {}).get("dehydratedState", {}).get("queries", [])
        for q in queries:
            state_data = q.get("state", {}).get("data", {})
            if not isinstance(state_data, dict):
                continue
            # Listings are in "private" and "agency" arrays
            for key in ("private", "agency", "platinum", "items", "feed_items"):
                lst = state_data.get(key)
                if isinstance(lst, list) and lst:
                    for item in lst:
                        if isinstance(item, dict) and item.get("token"):
                            items.append(item)
    except Exception as e:
        log.warning(f"Extract error: {e}")

    # Deduplicate by token
    seen = set()
    unique = []
    for item in items:
        tok = item.get("token")
        if tok and tok not in seen:
            seen.add(tok)
            unique.append(item)

    return unique


def extract_pagination(data):
    """Check if there are more pages."""
    try:
        queries = data.get("pageProps", {}).get("dehydratedState", {}).get("queries", [])
        for q in queries:
            sd = q.get("state", {}).get("data", {})
            if isinstance(sd, dict):
                pagination = sd.get("pagination", {})
                if pagination:
                    current = pagination.get("currentPage", 1)
                    total = pagination.get("totalPages", 1)
                    return current < total
    except:
        pass
    return False


# ============================================================================
# PARSING
# ============================================================================

def parse_listing(item, cfg):
    """Parse a single Yad2 Next.js listing into our format."""
    s = cfg["search"]
    addr = item.get("address", {})

    # --- ID ---
    token = item.get("token", "")
    if not token:
        return None

    # --- Rooms ---
    details = item.get("additionalDetails", {})
    rooms = details.get("roomsCount")
    if rooms is not None and not (s["rooms_min"] <= rooms <= s["rooms_max"]):
        return None

    # --- Floor ---
    house = addr.get("house", {})
    floor = house.get("floor")
    if s["exclude_ground_floor"] and floor is not None and floor == 0:
        return None

    # --- Price ---
    price = item.get("price")
    if not price or price < s["price_min"] or price > s["price_max"]:
        return None

    # --- Location ---
    street = addr.get("street", {}).get("text", "")
    neighborhood = addr.get("neighborhood", {}).get("text", "")
    city = addr.get("city", {}).get("text", "תל אביב יפו")
    house_num = house.get("number", "")
    address_str = ", ".join(filter(None, [
        f"{street} {house_num}".strip(), neighborhood, city
    ]))

    # --- Area match ---
    area_match = match_area(address_str, neighborhood, street, cfg)

    # --- Size ---
    size = details.get("squareMeter")
    meta = item.get("metaData", {})
    if not size:
        size = meta.get("squareMeterBuild")
    min_sqm = s.get("min_sqm", 0)
    if min_sqm and size is not None and size < min_sqm:
        return None

    # --- Images ---
    images = meta.get("images", [])
    cover = meta.get("coverImage", "")
    if cover and cover not in images:
        images.insert(0, cover)

    # --- Tags / amenities ---
    tags = item.get("tags", [])
    tag_names = " ".join(t.get("name", "") for t in tags if isinstance(t, dict)).lower()

    parking = "חנייה" in tag_names or "חניה" in tag_names or "parking" in tag_names
    elevator = bool("מעלית" in tag_names or "elevator" in tag_names or
                details.get("elevator") or details.get("hasElevator"))
    balcony = bool("מרפסת" in tag_names or "balcony" in tag_names or
               details.get("balcony") or details.get("hasBalcony"))
    ac = "מיזוג" in tag_names or "מזגן" in tag_names
    mamad = 'ממ"ד' in tag_names or "ממד" in tag_names

    # --- Elevator filter (note: Yad2 feed tags don't always include amenities,
    #     so this may filter aggressively. Set require_elevator=false if too strict) ---
    if s.get("require_elevator") and not elevator:
        return None

    # --- Property type ---
    prop_type = details.get("property", {}).get("text", "")

    # --- Ad type ---
    ad_type = item.get("adType", "")
    is_agent = ad_type == "agency" or ad_type == "business"

    # --- Entry date ---
    entry_date = item.get("entryDate", "") or item.get("dateOfEntry", "")

    # --- Coords ---
    coords = addr.get("coords", {})

    return {
        "item_id": token,
        "title": f"{prop_type} {street} {house_num}".strip() or address_str,
        "address": address_str,
        "street": f"{street} {house_num}".strip(),
        "neighborhood": neighborhood,
        "city": city,
        "rooms": rooms,
        "floor": floor,
        "total_floors": None,
        "price": price,
        "price_before": item.get("priceBeforeTag"),
        "size_sqm": size,
        "parking": parking,
        "elevator": elevator,
        "balcony": balcony,
        "ac": ac,
        "mamad": mamad,
        "furnished": "",
        "entry_date": entry_date,
        "description": "",
        "images": images,
        "link": ITEM_URL.format(token),
        "contact_name": "",
        "is_agent": is_agent,
        "area_match": area_match,
        "tags": [t.get("name", "") for t in tags if isinstance(t, dict)],
        "lat": coords.get("lat"),
        "lon": coords.get("lon"),
        "_raw": item,
    }


def match_area(address, neighborhood, street, cfg):
    text = f"{address} {neighborhood} {street}".lower()
    for kw in cfg["target_areas"]["hebrew"] + cfg["target_areas"]["english"]:
        if kw.lower() in text:
            return kw
    return ""


# ============================================================================
# DATABASE
# ============================================================================

def init_db():
    c = sqlite3.connect(DB)
    c.execute("PRAGMA journal_mode=WAL")
    c.execute("""CREATE TABLE IF NOT EXISTS apartments (
        item_id TEXT PRIMARY KEY, title TEXT, address TEXT, street TEXT,
        neighborhood TEXT, city TEXT, rooms REAL, floor INTEGER,
        total_floors INTEGER, price INTEGER, price_before INTEGER,
        size_sqm INTEGER, parking INT DEFAULT 0, elevator INT DEFAULT 0,
        balcony INT DEFAULT 0, ac INT DEFAULT 0, mamad INT DEFAULT 0,
        furnished TEXT, entry_date TEXT, description TEXT, images TEXT,
        link TEXT, contact_name TEXT, is_agent INT DEFAULT 0,
        first_seen TEXT, last_seen TEXT, is_new INT DEFAULT 1,
        area_match TEXT, tags TEXT, lat REAL, lon REAL, raw_json TEXT
    )""")
    c.execute("""CREATE TABLE IF NOT EXISTS scan_log (
        id INTEGER PRIMARY KEY AUTOINCREMENT, ts TEXT, total INT, new INT
    )""")
    c.commit()
    return c

def upsert(c, a):
    now = datetime.now().isoformat()
    if c.execute("SELECT 1 FROM apartments WHERE item_id=?", (a["item_id"],)).fetchone():
        c.execute("UPDATE apartments SET last_seen=?, price=? WHERE item_id=?",
                  (now, a.get("price"), a["item_id"]))
        c.commit()
        return False
    c.execute(
        """INSERT INTO apartments (item_id,title,address,street,neighborhood,city,
           rooms,floor,total_floors,price,price_before,size_sqm,
           parking,elevator,balcony,ac,mamad,furnished,entry_date,description,
           images,link,contact_name,is_agent,first_seen,last_seen,is_new,
           area_match,tags,lat,lon,raw_json)
           VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,1,?,?,?,?,?)""",
        (a["item_id"], a.get("title",""), a.get("address",""), a.get("street",""),
         a.get("neighborhood",""), a.get("city",""), a.get("rooms"), a.get("floor"),
         a.get("total_floors"), a.get("price"), a.get("price_before"), a.get("size_sqm"),
         int(a.get("parking",False)), int(a.get("elevator",False)),
         int(a.get("balcony",False)), int(a.get("ac",False)), int(a.get("mamad",False)),
         a.get("furnished",""), a.get("entry_date",""), a.get("description",""),
         json.dumps(a.get("images",[]),ensure_ascii=False), a.get("link",""),
         a.get("contact_name",""), int(a.get("is_agent",False)),
         now, now,
         a.get("area_match",""), json.dumps(a.get("tags",[]),ensure_ascii=False),
         a.get("lat"), a.get("lon"),
         json.dumps(a.get("_raw",{}),ensure_ascii=False))
    )
    c.commit()
    return True

def export_json(c, cfg):
    cols = [r[1] for r in c.execute("PRAGMA table_info(apartments)").fetchall()]
    rows = c.execute("SELECT * FROM apartments ORDER BY is_new DESC, first_seen DESC").fetchall()
    apts = []
    for r in rows:
        d = dict(zip(cols, r))
        d["images"] = json.loads(d.get("images") or "[]")
        d["tags"] = json.loads(d.get("tags") or "[]")
        for bf in ("parking","elevator","balcony","ac","mamad","is_new","is_agent"):
            d[bf] = bool(d.get(bf))
        d.pop("raw_json", None)
        apts.append(d)
    apts.sort(key=lambda a: (not a["is_new"], not a["parking"], a.get("price") or 99999))
    s = cfg["search"]
    payload = {
        "updated": datetime.now().isoformat(),
        "count": len(apts),
        "config": {
            "rooms": f"{s['rooms_min']}-{s['rooms_max']}",
            "price": f"₪{s['price_min']:,}-{s['price_max']:,}",
            "areas": cfg["target_areas"]["english"],
        },
        "apartments": apts,
    }
    with open(OUT, "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, indent=2)
    log.info(f"Exported {len(apts)} apartments")


# ============================================================================
# MAIN SCRAPE
# ============================================================================

def scrape(cfg):
    s = cfg["search"]
    log.info(f"HTTP: {HTTP_LIB} | rooms={s['rooms_min']}-{s['rooms_max']} | "
             f"price=₪{s['price_min']:,}-{s['price_max']:,}")

    # Step 1: Get build ID
    build_id = get_build_id()
    if not build_id:
        log.error("Cannot get build ID — aborting")
        return []

    seen, apts = set(), []
    delay = cfg["schedule"]["delay_between_requests_sec"]

    # Step 2: Query each neighborhood group, plus a broad query
    neighborhood_groups = [
        ("NW District", [20060009]),   # ב' / צפון מערב העיר
        ("Neve Amit", [1211]),         # נווה עמית
        ("Weizmann / Achuzat", [1236, 1235]),  # מכון ויצמן + אחוזת הנשיא
        ("Chavatzelet / Neve Yehuda", [1216, 1213]),  # חבצלת + נווה יהודה
        ("Broad (no filter)", None),   # All Rehovot
    ]

    for area_name, nhood_ids in neighborhood_groups:
        log.info(f"Querying: {area_name}")
        page = 1
        while page <= cfg["schedule"]["max_pages"]:
            url = build_feed_url(build_id, cfg, page=page, neighborhoods=nhood_ids)
            time.sleep(delay + random.uniform(0, 1.0))
            data = fetch(url)
            if not data:
                break

            items = extract_listings(data)
            if not items:
                log.info(f"  p{page}: 0 items — stopping")
                break

            n = 0
            for raw in items:
                apt = parse_listing(raw, cfg)
                if apt and apt["item_id"] not in seen:
                    seen.add(apt["item_id"])
                    apts.append(apt)
                    n += 1

            log.info(f"  p{page}: {len(items)} items → {n} new matches")

            if not extract_pagination(data):
                break
            page += 1

        # Extra delay between neighborhood groups to avoid connection resets
        time.sleep(3 + random.uniform(1, 2))

    # Step 3: Area keyword filter (for broad query results)
    keywords = cfg["target_areas"]["hebrew"] + cfg["target_areas"]["english"]
    filtered = []
    for a in apts:
        if a["area_match"]:
            filtered.append(a)
        else:
            text = f"{a['address']} {a['neighborhood']} {a['street']}".lower()
            for kw in keywords:
                if kw.lower() in text:
                    a["area_match"] = kw
                    filtered.append(a)
                    break

    log.info(f"Total: {len(apts)} → area filtered: {len(filtered)}")
    return filtered


# ============================================================================
# NOTIFICATIONS
# ============================================================================

def notify_tg(new_apts, cfg):
    n = cfg["notifications"]
    if not n.get("telegram_enabled") or not n.get("telegram_bot_token"):
        return
    import requests as rq
    for a in new_apts[:10]:
        p = "🅿️" if a["parking"] else ""
        msg = (f"🏠 <b>דירה חדשה!</b>\n📍 {a['address']}\n"
               f"🛏 {a.get('rooms','?')} חד׳ | קומה {a.get('floor','?')}\n"
               f"💰 ₪{a['price']:,} {p}\n🔗 <a href=\"{a['link']}\">יד2</a>")
        try:
            rq.post(f"https://api.telegram.org/bot{n['telegram_bot_token']}/sendMessage",
                    json={"chat_id":n["telegram_chat_id"],"text":msg,"parse_mode":"HTML"}, timeout=10)
            time.sleep(0.5)
        except: pass

def notify_email(new_apts, cfg):
    n = cfg["notifications"]
    if not n.get("email_enabled") or not n.get("email_smtp"):
        return
    lines = [f"נמצאו {len(new_apts)} דירות חדשות:\n"]
    for a in new_apts[:20]:
        lines.append(f"• {a['address']} | {a.get('rooms','?')} חד׳ | ₪{a['price']:,} | "
                     f"קומה {a.get('floor','?')} | {'🅿️' if a['parking'] else ''}\n  {a['link']}")
    body = "\n".join(lines)
    msg = MIMEText(body, "plain", "utf-8")
    msg["Subject"] = f"🏠 Yad2: {len(new_apts)} דירות חדשות"
    msg["From"] = n["email_user"]; msg["To"] = n["email_to"]
    try:
        with smtplib.SMTP(n["email_smtp"], n["email_port"]) as s:
            s.starttls(); s.login(n["email_user"], n["email_pass"]); s.send_message(msg)
        log.info("Email sent")
    except Exception as e:
        log.warning(f"Email err: {e}")


# ============================================================================
# RUN
# ============================================================================

def run_once():
    cfg = load_cfg()
    conn = init_db()
    log.info("=" * 60 + "\nSCAN CYCLE\n" + "=" * 60)
    apts = scrape(cfg)
    new_c, new_a = 0, []
    for a in apts:
        if upsert(conn, a):
            new_c += 1; new_a.append(a)
            p = "✓" if a["parking"] else "✗"
            log.info(f"  🆕 {a['address']} | {a.get('rooms','?')}r | ₪{a['price']:,} | "
                     f"F{a.get('floor','?')} | P:{p} | {a.get('area_match','')}")
    export_json(conn, cfg)
    conn.execute("INSERT INTO scan_log (ts,total,new) VALUES (?,?,?)",
                 (datetime.now().isoformat(), len(apts), new_c))
    conn.commit()
    log.info(f"Done: {len(apts)} found, {new_c} new")
    if new_a:
        notify_tg(new_a, cfg)
        notify_email(new_a, cfg)
    conn.close()
    return new_c

def main():
    cfg = load_cfg()
    hrs = cfg["schedule"]["interval_hours"]
    log.info(f"Yad2 Bot v3.0 | interval={hrs}h | DB={DB}")
    while True:
        try: run_once()
        except KeyboardInterrupt: break
        except Exception as e: log.error(f"Error: {e}", exc_info=True)
        log.info(f"Next in {hrs}h...")
        try: time.sleep(hrs * 3600)
        except KeyboardInterrupt: break

if __name__ == "__main__":
    if "--reset" in sys.argv:
        for p in (DB, OUT): p.unlink(missing_ok=True)
        log.info("Reset done.")
    elif "--debug" in sys.argv:
        log.info("=" * 60)
        log.info(f"DEBUG | HTTP: {HTTP_LIB}")
        log.info("=" * 60)
        cfg = load_cfg()

        # Test build ID
        bid = get_build_id()
        if not bid:
            log.error("FAILED: Cannot get build ID")
            sys.exit(1)

        # Test one feed request
        url = build_feed_url(bid, cfg, page=1, neighborhoods=[1483, 1484])
        log.info(f"Feed URL: {url}")
        data = fetch(url)
        if not data:
            log.error("FAILED: Cannot fetch feed")
            sys.exit(1)

        items = extract_listings(data)
        log.info(f"✓ Found {len(items)} raw listings")
        if items:
            first = items[0]
            addr = first.get("address", {})
            log.info(f"Sample: token={first.get('token')}, "
                     f"price={first.get('price')}, "
                     f"rooms={first.get('additionalDetails',{}).get('roomsCount')}, "
                     f"street={addr.get('street',{}).get('text')}, "
                     f"neighborhood={addr.get('neighborhood',{}).get('text')}, "
                     f"floor={addr.get('house',{}).get('floor')}")
            # Parse it
            parsed = parse_listing(first, cfg)
            if parsed:
                log.info(f"Parsed: {parsed['address']} | ₪{parsed['price']:,} | "
                         f"{parsed['rooms']}r | F{parsed['floor']} | "
                         f"imgs={len(parsed['images'])} | area={parsed['area_match']}")
        log.info("=" * 60)
        log.info("✓ Everything works! Run: python scraper.py --once")
    elif "--once" in sys.argv:
        run_once()
    else:
        main()
