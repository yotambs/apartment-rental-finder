#!/usr/bin/env python3
"""
Multi-source rental scraper. Reads a YAML config, fetches listings from
Madlan and Yad2, filters by neighborhood, writes an HTML report, and
optionally emails it.

Config (YAML):
  town: rehovot
  neighborhoods: [דניה]
  sources: [madlan, yad2]                                  # optional, defaults to all
  email: you@example.com
  filters: {min_price, max_price, min_rooms, max_rooms}    # optional
  smtp:   {host, port, user, password}                     # optional

Output:
  rental_<town>_<YYYY-MM-DD>.html   in the script directory

Usage:
  python my_scarper.py                              # uses ./search_config.yaml
  python my_scarper.py --config path/to/file.yaml
"""

import argparse, html as htmllib, json, re, smtplib, sys
from datetime import date
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from pathlib import Path
from urllib.parse import quote

import yaml
from scrapling.fetchers import StealthyFetcher

try:
    from curl_cffi import requests as http
    HTTP_LIB = "curl_cffi"
except ImportError:
    import requests as http
    HTTP_LIB = "requests"

DIR = Path(__file__).parent

# Map of supported towns. Add more entries as needed.
#   madlan_slug: the Hebrew place token in Madlan's /for-rent/<slug> path.
#   yad2_city_code + yad2_slug: parameters for the Yad2 _next/data feed.
TOWNS = {
    "rehovot": {
        "display": "Rehovot",
        "madlan_slug": "רחובות-ישראל",
        "yad2_city_code": "8400",
        "yad2_slug": "center-and-sharon",
    },
    "tel-aviv": {
        "display": "Tel Aviv",
        "madlan_slug": "תל-אביב-יפו-ישראל",
        "yad2_city_code": "5000",
        "yad2_slug": "tel-aviv-area",
    },
}

DEFAULT_FILTERS = {"min_price": 0, "max_price": 10000, "min_rooms": 0, "max_rooms": 10}


# ============================================================================
# MADLAN
# ============================================================================

PROPERTY_TYPES = "flat,gardenApartment,villa,cottage,dualCottage,attic,penthouse"
MADLAN_IMG_CDN = "https://images2.madlan.co.il/t:nonce:v=2;resize:height=328;convert:type=webp/"


def _madlan_url(town_info, f):
    place = quote(town_info["madlan_slug"], safe="")
    types = quote(PROPERTY_TYPES, safe=",")
    rooms_seg = f"{f['min_rooms']}-{f['max_rooms']}" if (f["min_rooms"] or f["max_rooms"] != 10) else ""
    slug = f"_0-45000____{types}__{rooms_seg}____{f['min_price']}-{f['max_price']}_______search-filter-top-bar"
    return (
        f"https://www.madlan.co.il/for-rent/{place}"
        f"?filters={quote(slug, safe='')}&marketplace=residential"
    )


def _madlan_extract_blob(page_html):
    anchor = "window.__SSR_HYDRATED_CONTEXT__="
    start = page_html.find(anchor)
    if start == -1:
        return None
    i = page_html.find("{", start)
    if i == -1:
        return None
    depth, in_str, esc = 0, False, False
    for j in range(i, len(page_html)):
        ch = page_html[j]
        if in_str:
            if esc:
                esc = False
            elif ch == "\\":
                esc = True
            elif ch == '"':
                in_str = False
            continue
        if ch == '"':
            in_str = True
        elif ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                return page_html[i : j + 1]
    return None


def fetch_madlan(town_info, filters):
    url = _madlan_url(town_info, filters)
    print(f"[madlan] fetching: {url}", flush=True)
    StealthyFetcher.adaptive = True
    page = StealthyFetcher.fetch(url, headless=True, network_idle=True)
    raw = getattr(page, "body", None) or str(page)
    page_html = raw.decode("utf-8", errors="replace") if isinstance(raw, (bytes, bytearray)) else raw
    blob = _madlan_extract_blob(page_html)
    if not blob:
        print("[madlan] no SSR blob found", flush=True)
        return []
    blob = re.sub(r'(?<![\w"])undefined(?![\w"])', "null", blob)
    try:
        data = json.loads(blob)
    except json.JSONDecodeError:
        print("[madlan] SSR blob JSON parse failed", flush=True)
        return []
    try:
        items = data["reduxInitialState"]["domainData"]["searchList"]["data"]["searchPoiV2"]["poi"]
    except (KeyError, TypeError):
        print("[madlan] poi path missing in SSR data", flush=True)
        return []
    out = [_madlan_normalize(it) for it in items if isinstance(it, dict)]
    print(f"[madlan] parsed {len(out)} listings", flush=True)
    return out


def _madlan_image(images):
    if not images:
        return ""
    first = images[0]
    path = first.get("imageUrl") if isinstance(first, dict) else (first if isinstance(first, str) else None)
    if not path:
        return ""
    return path if path.startswith("http") else MADLAN_IMG_CDN + path


def _madlan_normalize(item):
    addr = item.get("addressDetails") or {}
    lid = item.get("id") or ""
    address = item.get("address") or " ".join(
        str(x) for x in [addr.get("streetName"), addr.get("streetNumber"), addr.get("city")] if x
    ).strip()
    return {
        "source": "madlan",
        "id": lid,
        "price": item.get("price") or "",
        "rooms": item.get("beds") or "",
        "size": item.get("area") or "",
        "floor": item.get("floor"),
        "address": address,
        "neighborhood": addr.get("neighbourhood") or "",
        "condition": item.get("generalCondition") or "",
        "image": _madlan_image(item.get("images") or []),
        "link": f"https://www.madlan.co.il/listings/{lid}" if lid else "",
    }


# ============================================================================
# YAD2
# ============================================================================

YAD2_BASE = "https://www.yad2.co.il"
YAD2_HEADERS = {
    "User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 Chrome/126.0.0.0 Safari/537.36",
    "Accept": "application/json, text/html, */*",
    "Accept-Language": "he-IL,he;q=0.9,en;q=0.7",
    "Referer": f"{YAD2_BASE}/realestate/rent",
}


def _yad2_get(url):
    kwargs = {"headers": YAD2_HEADERS, "timeout": 30}
    if HTTP_LIB == "curl_cffi":
        kwargs["impersonate"] = "chrome124"
    try:
        return http.get(url, **kwargs)
    except Exception as e:
        print(f"[yad2] GET {url[:80]} failed: {e}", flush=True)
        return None


def _yad2_build_id():
    r = _yad2_get(f"{YAD2_BASE}/realestate/rent")
    if not r or getattr(r, "status_code", 0) != 200:
        return None
    text = r.text
    for pattern in (r'/_next/data/([a-zA-Z0-9_-]+)/', r'"buildId"\s*:\s*"([^"]+)"'):
        m = re.search(pattern, text)
        if m:
            return m.group(1)
    return None


def fetch_yad2(town_info, filters):
    print(f"[yad2] fetching for {town_info['display']}", flush=True)
    build_id = _yad2_build_id()
    if not build_id:
        print("[yad2] could not extract build ID", flush=True)
        return []
    print(f"[yad2] build id: {build_id}", flush=True)

    f = filters
    params = {
        "minRooms": str(f["min_rooms"]),
        "maxRooms": str(f["max_rooms"]),
        "minPrice": str(f["min_price"]),
        "maxPrice": str(f["max_price"]),
        "multiCity": town_info["yad2_city_code"],
        "slug": town_info["yad2_slug"],
    }
    qs = "&".join(f"{k}={v}" for k, v in params.items())
    url = f"{YAD2_BASE}/realestate/_next/data/{build_id}/rent/{town_info['yad2_slug']}.json?{qs}"

    r = _yad2_get(url)
    if not r or getattr(r, "status_code", 0) != 200:
        print(f"[yad2] feed HTTP {getattr(r, 'status_code', '?')}", flush=True)
        return []
    try:
        data = r.json()
    except Exception as e:
        print(f"[yad2] feed JSON parse failed: {e}", flush=True)
        return []

    raw = []
    for q in data.get("pageProps", {}).get("dehydratedState", {}).get("queries", []):
        sd = q.get("state", {}).get("data", {})
        if not isinstance(sd, dict):
            continue
        for key in ("private", "agency", "platinum", "items", "feed_items"):
            lst = sd.get(key)
            if isinstance(lst, list):
                raw.extend(it for it in lst if isinstance(it, dict) and it.get("token"))

    seen, items = set(), []
    for it in raw:
        tok = it.get("token")
        if tok in seen:
            continue
        seen.add(tok)
        items.append(_yad2_normalize(it))
    print(f"[yad2] parsed {len(items)} listings", flush=True)
    return items


def _yad2_normalize(item):
    addr = item.get("address") or {}
    house = addr.get("house") or {}
    details = item.get("additionalDetails") or {}
    meta = item.get("metaData") or {}
    street = (addr.get("street") or {}).get("text") or ""
    house_num = house.get("number") or ""
    neighborhood = (addr.get("neighborhood") or {}).get("text") or ""
    city = (addr.get("city") or {}).get("text") or ""
    images = meta.get("images") or []
    image = meta.get("coverImage") or (images[0] if images else "")
    token = item.get("token") or ""
    return {
        "source": "yad2",
        "id": token,
        "price": item.get("price") or "",
        "rooms": details.get("roomsCount") or "",
        "size": details.get("squareMeter") or meta.get("squareMeterBuild") or "",
        "floor": house.get("floor"),
        "address": ", ".join(filter(None, [f"{street} {house_num}".strip(), neighborhood, city])),
        "neighborhood": neighborhood,
        "condition": (details.get("property") or {}).get("text") or "",
        "image": image,
        "link": f"{YAD2_BASE}/item/{token}" if token else "",
    }


# ============================================================================
# FILTER + RENDER + EMAIL
# ============================================================================

def filter_by_neighborhood(items, names):
    if not names:
        return items
    needles = [n.strip().lower() for n in names if n and n.strip()]
    if not needles:
        return items
    out = []
    for it in items:
        text = f"{it['neighborhood']} {it['address']}".lower()
        if any(n in text for n in needles):
            out.append(it)
    return out


def render_html(town_display, items, today, neighborhoods, sources):
    cards = []
    for it in items:
        img_html = (
            f'<img src="{htmllib.escape(it["image"], quote=True)}" alt="" loading="lazy" />'
            if it["image"]
            else '<div class="noimg">no image</div>'
        )
        link_html = (
            f'<a href="{htmllib.escape(it["link"], quote=True)}" target="_blank" rel="noopener">Open ↗</a>'
            if it["link"]
            else ""
        )
        try:
            price_str = f"₪{int(it['price']):,}"
        except (TypeError, ValueError):
            price_str = htmllib.escape(str(it["price"])) if it["price"] else "—"
        floor = it["floor"] if it["floor"] not in (None, "") else "—"
        extras = " · ".join(filter(None, [
            htmllib.escape(str(it["neighborhood"])) if it["neighborhood"] else "",
            htmllib.escape(str(it["condition"])) if it["condition"] else "",
        ]))
        cards.append(f"""    <div class="card">
      {img_html}
      <div class="meta">
        <div class="row"><span class="price">{price_str}</span><span class="src src-{it['source']}">{it['source']}</span></div>
        <div class="addr">{htmllib.escape(str(it['address']))}</div>
        <div class="info">{htmllib.escape(str(it['rooms']))} rooms · {htmllib.escape(str(it['size']))} m² · floor {htmllib.escape(str(floor))}</div>
        <div class="info">{extras}</div>
        {link_html}
      </div>
    </div>""")

    grid = "\n".join(cards) if cards else '    <p class="empty">No listings matched.</p>'
    nh = ", ".join(neighborhoods) if neighborhoods else "all neighborhoods"
    return f"""<!DOCTYPE html>
<html lang="he" dir="rtl">
<head>
<meta charset="UTF-8">
<title>Rental — {town_display} — {today}</title>
<style>
  * {{ box-sizing: border-box; }}
  body {{ font-family: 'Heebo', system-ui, sans-serif; background: #0b0b0f; color: #e4e2de; margin: 0; padding: 2rem; }}
  h1 {{ margin: 0 0 .25rem; font-size: 1.6rem; }}
  .sub {{ color: #7c7c84; font-size: .9rem; margin-bottom: 1.5rem; }}
  .grid {{ display: grid; grid-template-columns: repeat(auto-fill, minmax(280px, 1fr)); gap: 1rem; }}
  .card {{ background: #1a1a22; border: 1px solid #2a2a36; border-radius: 10px; overflow: hidden; display: flex; flex-direction: column; }}
  .card img, .noimg {{ width: 100%; height: 180px; object-fit: cover; background: #131318; color: #55555c; display: flex; align-items: center; justify-content: center; font-size: .9rem; }}
  .meta {{ padding: 1rem; display: flex; flex-direction: column; gap: .35rem; }}
  .row {{ display: flex; justify-content: space-between; align-items: center; }}
  .price {{ font-size: 1.3rem; font-weight: 700; color: #fbbf24; }}
  .src {{ font-size: .7rem; padding: .15rem .5rem; border-radius: 999px; text-transform: uppercase; letter-spacing: .05em; }}
  .src-madlan {{ background: #233; color: #00d4aa; }}
  .src-yad2 {{ background: #322; color: #ff9a55; }}
  .addr {{ font-weight: 500; }}
  .info {{ color: #7c7c84; font-size: .9rem; }}
  a {{ color: #00d4aa; text-decoration: none; margin-top: .4rem; font-size: .9rem; }}
  a:hover {{ text-decoration: underline; }}
  .empty {{ color: #7c7c84; }}
</style>
</head>
<body>
<h1>{town_display} rentals — {today}</h1>
<p class="sub">{len(items)} listings · {htmllib.escape(nh)} · sources: {htmllib.escape(' + '.join(sources))}</p>
<div class="grid">
{grid}
</div>
</body>
</html>
"""


def send_email(html, to, subject, smtp):
    if not (smtp and smtp.get("host") and to):
        return False
    msg = MIMEMultipart("alternative")
    msg["Subject"] = subject
    msg["From"] = smtp.get("user") or to
    msg["To"] = to
    msg.attach(MIMEText("HTML version attached. Open this email in a client that supports HTML.", "plain", "utf-8"))
    msg.attach(MIMEText(html, "html", "utf-8"))
    try:
        with smtplib.SMTP(smtp["host"], int(smtp.get("port", 587))) as s:
            s.starttls()
            if smtp.get("user") and smtp.get("password"):
                s.login(smtp["user"], smtp["password"])
            s.send_message(msg)
        return True
    except Exception as e:
        print(f"[email] failed: {e}", flush=True)
        return False


# ============================================================================
# MAIN
# ============================================================================

SOURCES = {
    "madlan": fetch_madlan,
    "yad2": fetch_yad2,
}


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--config", default=str(DIR / "search_config.yaml"))
    args = ap.parse_args()

    cfg_path = Path(args.config)
    if not cfg_path.exists():
        print(f"Config not found: {cfg_path}", file=sys.stderr)
        return 1
    cfg = yaml.safe_load(cfg_path.read_text(encoding="utf-8")) or {}

    town_key = (cfg.get("town") or "").strip().lower()
    if town_key not in TOWNS:
        print(f"Unknown town '{town_key}'. Add it to TOWNS in {Path(__file__).name}. "
              f"Known: {sorted(TOWNS)}", file=sys.stderr)
        return 1
    town_info = TOWNS[town_key]

    filters = {**DEFAULT_FILTERS, **(cfg.get("filters") or {})}
    neighborhoods = cfg.get("neighborhoods") or []
    if isinstance(neighborhoods, str):
        neighborhoods = [neighborhoods]

    sources = cfg.get("sources") or list(SOURCES)
    if isinstance(sources, str):
        sources = [sources]
    sources = [s.strip().lower() for s in sources if s and s.strip()]
    unknown = [s for s in sources if s not in SOURCES]
    if unknown:
        print(f"Unknown sources: {unknown}. Known: {sorted(SOURCES)}", file=sys.stderr)
        return 1
    if not sources:
        sources = list(SOURCES)

    items = []
    for src in sources:
        items.extend(SOURCES[src](town_info, filters))
    print(f"[total] {len(items)} raw listings from {sources}", flush=True)

    filtered = filter_by_neighborhood(items, neighborhoods)
    filtered.sort(key=lambda x: (x["price"] if isinstance(x["price"], (int, float)) else 10**9))
    print(f"[filter] {len(filtered)} after neighborhood match", flush=True)

    today = date.today().isoformat()
    html = render_html(town_info["display"], filtered, today, neighborhoods, sources)
    out = DIR / f"rental_{town_key}_{today}.html"
    out.write_text(html, encoding="utf-8")
    print(f"[write] {out}", flush=True)

    email_to = cfg.get("email")
    smtp = cfg.get("smtp")
    if email_to and smtp and smtp.get("host"):
        if send_email(html, email_to, f"Rentals — {town_info['display']} — {today}", smtp):
            print(f"[email] sent to {email_to}", flush=True)
    elif email_to:
        print(f"[email] skipped — no SMTP config (would have sent to {email_to})", flush=True)

    return 0


if __name__ == "__main__":
    sys.exit(main())
