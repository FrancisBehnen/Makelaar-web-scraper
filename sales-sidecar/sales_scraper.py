"""Standalone sales scraper: apartments FOR SALE (koop) in Delft.

Independent of the rental python-sidecar. Scrapes four koop sources, keeps
listings priced <= EUR 270.000 with >= 2 rooms in the city of Delft, stores
them in its own SQLite file and notifies a Telegram group directly. The rental
db.sqlite is never touched.

Ported from python-sidecar/scraper.py (fetch infrastructure, parsers) and
responder/tg.py (notification helpers). Because the images are independent
there is no shared library — the relevant pieces are copied in.
"""

import json
import logging
import os
import re
import sqlite3
import sys
import time
import urllib.request
import xml.etree.ElementTree as ET
from concurrent.futures import ThreadPoolExecutor, TimeoutError
from pathlib import Path

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

FETCH_TIMEOUT = int(os.environ.get("FETCH_TIMEOUT", "120"))
MAX_CONSECUTIVE_FETCH_FAILURES = int(
    os.environ.get("MAX_CONSECUTIVE_FETCH_FAILURES", "3")
)
TIMEOUT_ALERT_THROTTLE_SECONDS = int(
    os.environ.get("TIMEOUT_ALERT_THROTTLE_SECONDS", str(6 * 3600))
)
_fetch_pool = ThreadPoolExecutor(max_workers=1)
_consecutive_fetch_failures = 0
_failed_urls_in_streak: set[str] = set()
_last_timeout_alert: dict[str, float] = {}

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
)
log = logging.getLogger("sales-sidecar")


class _SuppressBenignCF(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        return "No Cloudflare challenge found" not in record.getMessage()


logging.getLogger("scrapling").addFilter(_SuppressBenignCF())

DB_PATH = os.environ.get("SALES_DB_PATH", "data/sales.sqlite")
RECHECK_BATCH_SIZE = int(os.environ.get("RECHECK_BATCH_SIZE", "5"))
# Dropped on the data volume just before a self-restart exit; its presence on
# the next boot means the previous process self-restarted.
SELF_RESTART_MARKER = Path(DB_PATH).parent / ".self_restart_sales"
CHECK_INTERVAL = int(os.environ.get("CHECK_INTERVAL", "600"))
DEBUG_DUMP = os.environ.get("DEBUG_DUMP", "").lower() in ("1", "true", "yes")

TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "")
# Listing notifications go here (the koop Telegram group).
TELEGRAM_SALES_CHAT_IDS = os.environ.get("TELEGRAM_SALES_CHAT_IDS", "")
# Operational alerts (timeouts, self-restarts). Falls back to the sales group.
TELEGRAM_ALERT_CHAT_IDS = os.environ.get("TELEGRAM_ALERT_CHAT_IDS", "")

MAX_PRICE = 270000

# ---------------------------------------------------------------------------
# Source URLs
# ---------------------------------------------------------------------------

FUNDA_KOOP_URL = (
    "https://www.funda.nl/zoeken/koop"
    "?selected_area=%5B%22delft%22%5D&price=%220-270000%22"
)
PARARIUS_KOOP_URL = "https://www.pararius.nl/koopwoningen/delft/0-270000/page-{page}"
ZOMAKELAARS_KOOP_URL = (
    "https://www.zomakelaars.nl/aanbod/woningaanbod/Delft/koop/"
    "provincie-Zuid-Holland/"
)
VWMAKELAARS_KOOP_URL = "https://delft.vwmakelaars.nl/aanbod/woningaanbod/koop/"
ROEPMAN_KOOP_URL = (
    "https://www.roepman.nl/aanbod/woningaanbod/Delft/koop/" "provincie-Zuid-Holland/"
)
MORRIS_KOOP_URL = (
    "https://www.morrismakelaardij.nl/aanbod/woningaanbod/Delft/koop/"
    "provincie-Zuid-Holland/"
)
HOFVANDELFT_KOOP_URL = (
    "https://www.hofvandelft.nl/aanbod/woningaanbod/Delft/koop/"
    "provincie-Zuid-Holland/"
)

# Van Daal and Björnd expose a realtime-listings JSON feed (same shape the Bun
# app's RealtimeListingsJsonResponseProcessor consumes). Delft's biggest koop
# makelaars. Plain HTTP; no browser needed.
VANDAAL_FEED_URL = "https://www.vandaalmakelaardij.nl/nl/realtime-listings/consumer"
VANDAAL_BASE = "https://www.vandaalmakelaardij.nl"
BJORND_FEED_URL = "https://www.bjornd.nl/nl/realtime-listings/consumer"
BJORND_BASE = "https://www.bjornd.nl"

# Prinsenstad (Hayweb platform) publishes a per-segment sale sitemap. The
# residential-sale feed lists koop objects; the detail pages carry a
# "Vraagprijs" feature row and a status we gate on (skip Verkocht / Onder bod).
PRINSENSTAD_SALE_SITEMAP_URL = (
    "https://prinsenstadmakelaardij.nl/sitemap_listings_res_sale.xml"
)

# Olsthoorn Makelaars (custom WordPress "Sure" plugin, not Realworks/Hayweb).
# The /wonen/ grid has no server-side city/type filter reachable over plain
# HTTP — its search form posts to a JS-driven endpoint — so every paginated
# page is fetched and Delft is filtered client-side from each card.
OLSTHOORN_BASE = "https://www.olsthoornmakelaars.nl"
OLSTHOORN_WONEN_URL = f"{OLSTHOORN_BASE}/wonen/"
OLSTHOORN_WONEN_PAGE_URL = f"{OLSTHOORN_BASE}/wonen/page/{{page}}/"

# Van Silfhout Makelaars (WordPress + FacetWP). The /woningaanbod/ archive's
# first page is server-rendered, but later pages only exist behind FacetWP's
# REST refresh endpoint — called directly over plain HTTP with facets pinned
# to status=te-koop and locaties=delft so the koop/city filtering happens
# server-side and every returned card is already in scope.
VANSILFHOUT_BASE = "https://www.vansilfhout.nl"
VANSILFHOUT_REFRESH_URL = f"{VANSILFHOUT_BASE}/wp-json/facetwp/v1/refresh"

# De Bruyn en Tak (custom CMS, same card structure as rental sidecar).
# Server-rendered; the /te-koop/delft/ filter is URL-driven.
DEBRUYNENTAK_KOOP_URL = (
    "https://www.debruynentak.nl/aanbod/woningen/te-koop/delft/"
)

# Van Gulden Makelaardij (WordPress / Betheme, MTMO plugin).
# /aanbod/ lists predominantly koop; a stray huur listing with "per maand"
# in its price is filtered out. Server-rendered over plain HTTP.
VANGULDEN_KOOP_URL = "https://vanguldenmakelaardij.nl/aanbod/"

# Frisia Makelaars — same sitemap as the rental sidecar. The feed mixes
# huur and koop; the detail-page parser gates on a "Vraagprijs" feature
# block (the rental sidecar gates on "Huurprijs").
FRISIA_SITEMAP_URL = "https://frisiamakelaars.nl/sitemap/properties.xml"
FRISIA_MAX_FETCHES_PER_CYCLE = int(
    os.environ.get("FRISIA_MAX_FETCHES_PER_CYCLE", "10")
)

# Marloes Makelaars — WordPress property CPT sitemap. Mixes koop and
# huur; the rental sidecar keeps "per maand", the sales parser keeps
# "k.k." / "v.o.n.".
MARLOES_SITEMAP_URL = (
    "https://www.marloesmakelaars.nl/wp-sitemap-posts-property-1.xml"
)

# PSG Wonen (Hayweb platform, same as Prinsenstad). The residential-sale
# sitemap lists koop objects directly.
PSGWONEN_SALE_SITEMAP_URL = (
    "https://www.psg-wonen.nl/sitemap_listings_res_sale.xml"
)

_SITEMAP_NS = {"sm": "http://www.sitemaps.org/schemas/sitemap/0.9"}

# ---------------------------------------------------------------------------
# Filtering
# ---------------------------------------------------------------------------


def parse_price_euros(text: str) -> int | None:
    cleaned = re.sub(r"[^\d]", "", (text or "").replace(".", ""))
    return int(cleaned) if cleaned else None


def is_delft_city(text: str) -> bool:
    """True only when *text* mentions the city of Delft.

    Stricter than the rental sidecar's is_delft_area: matches "Delft" and
    "2611 AB Delft" but excludes Delfgauw / Den Hoorn / Rijswijk.
    """
    return bool(re.search(r"\bdelft\b", (text or "").lower()))


def parse_rooms(kamers: str) -> int | None:
    """Leading integer of a room string ("3 kamers" -> 3) or None."""
    m = re.match(r"\s*(\d+)", kamers or "")
    return int(m.group(1)) if m else None


def bedrooms_to_kamers(bedrooms: int) -> str:
    """Normalise a slaapkamers (bedroom) count to a total-kamers string.

    Dutch convention counts the living room as a kamer, so a 1-bedroom flat is
    a "2 kamer" apartment. Sources that expose bedrooms (Funda cards, the JSON
    feed's `bedrooms` field) go through this so every stored `kamers` value is
    consistently *total* rooms and the >= 2 gate in passes_filters means the
    same thing everywhere.
    """
    total = bedrooms + 1
    return "1 kamer" if total == 1 else f"{total} kamers"


# Non-dwelling listings (parking, storage, plots) that slip past a price/rooms
# filter. Matched case-insensitively against the address/title.
_JUNK_RE = re.compile(
    r"\b("
    r"parkeerplaats|parkeerplek|garagebox|garage|berging|"
    r"bouwgrond|kavel|opslag"
    r")\b",
    re.IGNORECASE,
)


def is_junk_listing(title: str) -> bool:
    """True when the address/title denotes a non-dwelling (parking, plot, …)."""
    return bool(_JUNK_RE.search(title or ""))


def passes_filters(h: dict[str, str]) -> bool:
    """Keep koop apartments in Delft, <= EUR 270.000, >= 2 rooms, no studios.

    - Price must be parseable and <= MAX_PRICE (unknown price -> exclude).
    - City must be Delft.
    - Rooms, when known, must be >= 2 (unknown rooms -> keep). Every source
      normalises its room count to *total kamers* before storing, so this gate
      is uniform (a 1-bedroom / 2-kamer flat passes; a studio / 1-kamer fails).
    - "studio" anywhere in the address -> exclude.
    - Parking spots, garages, storage boxes and building plots -> exclude.
    """
    price = parse_price_euros(h.get("vraagprijs", "") or "")
    if price is None or price > MAX_PRICE:
        return False

    city_text = h.get("plaats") or h.get("straatnaamHuisnummer") or ""
    if not is_delft_city(city_text):
        return False

    rooms = parse_rooms(h.get("kamers", "") or "")
    if rooms is not None and rooms < 2:
        return False

    address = (h.get("straatnaamHuisnummer", "") or "").lower()
    if "studio" in address:
        return False

    if is_junk_listing(h.get("straatnaamHuisnummer", "")):
        return False

    return True


def make_absolute(href: str, base: str) -> str:
    if not href:
        return ""
    if href.startswith("http"):
        return href
    if href.startswith("/"):
        from urllib.parse import urlparse

        parsed = urlparse(base)
        return f"{parsed.scheme}://{parsed.netloc}{href}"
    return f"{base.rstrip('/')}/{href}"


_PLAIN_HTTP_UA = (
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
)


def _http_get(url: str, timeout: int = 30) -> bytes:
    req = urllib.request.Request(url, headers={"User-Agent": _PLAIN_HTTP_UA})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return resp.read()


def _first_text(container, *selectors: str) -> str:
    for sel in selectors:
        els = container.css(sel)
        if els:
            txt = (els[0].text or "").strip()
            if txt:
                return txt
            txt = (els[0].get_all_text() or "").strip()
            if txt:
                return txt
    return ""


def _find_elements(page, *selectors: str):
    for sel in selectors:
        results = page.css(sel)
        if results:
            return results
    return []


# ---------------------------------------------------------------------------
# Database (own file — never the rental db.sqlite)
# ---------------------------------------------------------------------------


def init_db():
    Path(DB_PATH).parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(DB_PATH)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("""
        CREATE TABLE IF NOT EXISTS sales (
            url TEXT PRIMARY KEY,
            straatnaamHuisnummer TEXT,
            plaats TEXT,
            vraagprijs TEXT,
            oppervlakte TEXT,
            kamers TEXT,
            tg_message_ids TEXT DEFAULT '',
            status TEXT DEFAULT 'available'
        )
        """)
    cols = {row[1] for row in conn.execute("PRAGMA table_info(sales)")}
    if "tg_message_ids" not in cols:
        conn.execute("ALTER TABLE sales ADD COLUMN tg_message_ids TEXT DEFAULT ''")
    if "status" not in cols:
        conn.execute("ALTER TABLE sales ADD COLUMN status TEXT DEFAULT 'available'")
    conn.commit()
    return conn


def get_existing_urls(conn) -> set[str]:
    return {row[0] for row in conn.execute("SELECT url FROM sales")}


def table_is_empty(conn) -> bool:
    return conn.execute("SELECT COUNT(*) FROM sales").fetchone()[0] == 0


def save_house(conn, h: dict[str, str]) -> None:
    conn.execute(
        """
        INSERT OR IGNORE INTO sales
            (url, straatnaamHuisnummer, plaats, vraagprijs, oppervlakte, kamers)
        VALUES (?, ?, ?, ?, ?, ?)
        """,
        (
            h["url"],
            h["straatnaamHuisnummer"],
            h["plaats"],
            h["vraagprijs"],
            h["oppervlakte"],
            h["kamers"],
        ),
    )
    conn.commit()


def _norm_addr(text: str) -> str:
    return (text or "").lower().replace(" ", "")


def find_duplicate(conn, h: dict[str, str]) -> str | None:
    """URL of an existing listing at the same address+city, else None.

    Mirrors the responder's find_prior_response: addresses are normalised by
    stripping spaces and lowercasing; cities match on substring containment so
    "2624 NM Delft" and "Delft" are considered equal.
    """
    target_addr = _norm_addr(h.get("straatnaamHuisnummer", ""))
    target_city = (h.get("plaats", "") or "").lower()
    rows = conn.execute(
        "SELECT url, straatnaamHuisnummer, plaats FROM sales WHERE url != ?",
        (h["url"],),
    ).fetchall()
    for url, addr, plaats in rows:
        if _norm_addr(addr) != target_addr:
            continue
        city = (plaats or "").lower()
        # Empty string is a substring of anything, mirroring SQL LIKE '%%'.
        if target_city in city or city in target_city:
            return url
    return None


# ---------------------------------------------------------------------------
# Telegram (ported from responder/tg.py; urllib instead of requests)
# ---------------------------------------------------------------------------


def escape_html(text: str) -> str:
    """Escape characters special inside Telegram HTML messages."""
    return (text or "").replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def _status_button_row() -> list[dict]:
    """One row of status buttons matching the responder's stateless handler.

    Codes stay tiny (well under Telegram's 64-byte limit) and carry no id — the
    responder (the bot's only getUpdates consumer) dispatches them statelessly
    from the callback query's chat_id + message_id. Keep this JSON shape
    identical to ``responder.tg.status_button_row``.
    """
    return [
        {"text": "✅", "callback_data": "st:r"},  # gereageerd
        {"text": "📅", "callback_data": "st:i"},  # uitgenodigd
        {"text": "❌", "callback_data": "st:x"},  # afgewezen
        {"text": "🗑", "callback_data": "st:d"},  # niet interessant (delete)
    ]


def _status_keyboard() -> dict:
    return {"inline_keyboard": [_status_button_row()]}


def _send(chat_ids_raw: str, text: str, *, reply_markup: dict | None = None) -> list[dict]:
    """Send a Telegram message and return [{"chat_id": ..., "message_id": ...}, ...]."""
    sent: list[dict] = []
    if not TELEGRAM_BOT_TOKEN:
        return sent
    chat_ids = [c.strip() for c in (chat_ids_raw or "").split(",") if c.strip()]
    if not chat_ids:
        return sent
    api_url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    for chat_id in chat_ids:
        payload = {
            "chat_id": chat_id,
            "text": text,
            "parse_mode": "HTML",
            "disable_web_page_preview": True,
        }
        if reply_markup is not None:
            payload["reply_markup"] = reply_markup
        body = json.dumps(payload).encode()
        req = urllib.request.Request(
            api_url, data=body, headers={"Content-Type": "application/json"}
        )
        try:
            resp_body = urllib.request.urlopen(req, timeout=10).read()
            resp = json.loads(resp_body)
            if resp.get("ok") and "result" in resp:
                sent.append(
                    {
                        "chat_id": chat_id,
                        "message_id": resp["result"]["message_id"],
                    }
                )
        except Exception as exc:
            log.error("Telegram send to %s failed: %s", chat_id, exc)
    return sent


def _listing_text(h: dict[str, str]) -> str:
    return (
        "\U0001f3e1 <b>Nieuwe koopwoning in Delft!</b> \U0001f3e1\n\n"
        "<blockquote>Gegevens van de woning:\n"
        f"Adres: {escape_html(h['straatnaamHuisnummer'])}, "
        f"{escape_html(h['plaats'])}\n"
        f"Vraagprijs: {escape_html(h['vraagprijs'])}\n"
        f"Oppervlakte: {escape_html(h['oppervlakte'])}\n"
        f"Kamers: {escape_html(h['kamers'])}\n"
        f"URL: {escape_html(h['url'])}"
        "</blockquote>"
    )


def notify_new_listing(h: dict[str, str]) -> list[dict]:
    return _send(
        TELEGRAM_SALES_CHAT_IDS, _listing_text(h), reply_markup=_status_keyboard()
    )


def send_telegram_alert(message: str) -> None:
    _send(TELEGRAM_ALERT_CHAT_IDS or TELEGRAM_SALES_CHAT_IDS, message)


def send_throttled_timeout_alert(key: str, message: str) -> None:
    """Send a per-site timeout alert at most once per throttle window."""
    now = time.time()
    last = _last_timeout_alert.get(key, 0.0)
    if now - last < TIMEOUT_ALERT_THROTTLE_SECONDS:
        log.info(
            "Suppressing repeat timeout alert for %s (last sent %.0fs ago)",
            key,
            now - last,
        )
        return
    _last_timeout_alert[key] = now
    send_telegram_alert(message)


# ---------------------------------------------------------------------------
# Telegram message deletion (sold listings)
# ---------------------------------------------------------------------------

_cycle_sold_urls: set[str] = set()


def _record_sold_url(url: str) -> None:
    if url:
        _cycle_sold_urls.add(url)


def _delete_message(chat_id: str, message_id: int) -> bool:
    if not TELEGRAM_BOT_TOKEN:
        return False
    api_url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/deleteMessage"
    body = json.dumps({"chat_id": chat_id, "message_id": message_id}).encode()
    req = urllib.request.Request(
        api_url, data=body, headers={"Content-Type": "application/json"}
    )
    try:
        resp_body = urllib.request.urlopen(req, timeout=10).read()
        resp = json.loads(resp_body)
        return resp.get("ok", False)
    except Exception as exc:
        log.warning("Telegram delete %s/%s failed: %s", chat_id, message_id, exc)
        return False


def _delete_listing_messages(conn, url: str) -> str | None:
    """Delete Telegram messages for a listing and mark it as sold.

    Returns the address string if the listing was transitioned, else None.
    """
    row = conn.execute(
        "SELECT tg_message_ids, status, straatnaamHuisnummer FROM sales WHERE url = ?",
        (url,),
    ).fetchone()
    if row is None:
        return None
    tg_ids_raw, current_status, addr = row
    if current_status != "available":
        return None

    if tg_ids_raw:
        for entry in json.loads(tg_ids_raw):
            _delete_message(str(entry["chat_id"]), entry["message_id"])

    conn.execute("UPDATE sales SET status = 'sold' WHERE url = ?", (url,))
    conn.commit()

    log.info("Marked sold and deleted TG message(s): %s (%s)", addr, url)
    return addr or url


def process_sold_urls(conn, sold_urls: set[str]) -> list[str]:
    """Check sold URLs against DB and delete Telegram messages for matches.

    Returns list of addresses that were transitioned to sold.
    """
    removed: list[str] = []
    for url in sold_urls:
        addr = _delete_listing_messages(conn, url)
        if addr is not None:
            removed.append(addr)
    return removed


def recheck_available_listings(conn) -> list[str]:
    """Re-fetch a batch of available listings via plain HTTP and check status.

    Universal fallback for sources where sold detection doesn't happen
    during the normal scrape (sitemap detail pages, Funda, etc.).
    Returns list of addresses that were transitioned to sold.
    """
    rows = conn.execute(
        "SELECT url FROM sales WHERE status = 'available' "
        "ORDER BY rowid ASC LIMIT ?",
        (RECHECK_BATCH_SIZE,),
    ).fetchall()
    if not rows:
        return []

    removed: list[str] = []
    for (url,) in rows:
        try:
            body = _http_get(url, timeout=15)
        except Exception:
            continue
        text = body.decode("utf-8", errors="ignore")
        if _SOLD_STATUS_RE.search(text):
            addr = _delete_listing_messages(conn, url)
            if addr is not None:
                removed.append(addr)
    return removed


_last_sold_summary_ids: list[dict] = []


def _send_sold_summary(addresses: list[str]) -> None:
    """Send a summary of removed listings and delete the previous summary."""
    global _last_sold_summary_ids
    for entry in _last_sold_summary_ids:
        _delete_message(str(entry["chat_id"]), entry["message_id"])

    listing_lines = "\n".join(f"• {escape_html(a)}" for a in addresses)
    count = len(addresses)
    word = "woning" if count == 1 else "woningen"
    text = (
        f"\U0001f6d1 <b>{count} {word} verkocht/onder bod — "
        f"bericht(en) verwijderd</b>\n\n{listing_lines}"
    )
    _last_sold_summary_ids = _send(TELEGRAM_SALES_CHAT_IDS, text)


# ---------------------------------------------------------------------------
# Debug helper
# ---------------------------------------------------------------------------


def dump_html(page, name: str) -> None:
    if not DEBUG_DUMP:
        return
    dump_dir = Path(DB_PATH).parent / "debug"
    dump_dir.mkdir(parents=True, exist_ok=True)
    path = dump_dir / f"{name}.html"
    path.write_bytes(page.body)
    log.info("Dumped HTML to %s (%d bytes)", path, len(page.body))


# ---------------------------------------------------------------------------
# Parsers (ported from python-sidecar/scraper.py; city check -> is_delft_city)
# ---------------------------------------------------------------------------


def scrape_pararius_koop(page) -> list[dict[str, str]]:
    dump_html(page, "pararius_koop")
    houses: list[dict[str, str]] = []

    listings = page.css("li.search-list__item--listing")
    if not listings:
        listings = page.css("section.listing-search-item")
    log.info("Pararius koop: %d listing elements found", len(listings))

    for listing in listings:
        try:
            link_el = listing.css("a.listing-search-item__link--title")
            if not link_el:
                continue

            href = link_el[0].attrib.get("href", "")
            address = (link_el[0].text or "").strip()
            if not href:
                continue
            url = make_absolute(href, "https://www.pararius.nl")

            subtitle = listing.css(".listing-search-item__sub-title")
            city = (subtitle[0].text or "").strip() if subtitle else "Delft"

            if city and not is_delft_city(city):
                continue

            price_el = listing.css(".listing-search-item__price-main")
            price = (price_el[0].text or "").strip() if price_el else ""

            area_el = listing.css(".illustrated-features__item--surface-area")
            area = (area_el[0].text or "").strip() if area_el else ""

            rooms_el = listing.css(".illustrated-features__item--number-of-rooms")
            rooms = (rooms_el[0].text or "").strip() if rooms_el else ""

            houses.append(
                {
                    "url": url,
                    "straatnaamHuisnummer": address,
                    "plaats": city,
                    "vraagprijs": price,
                    "oppervlakte": area,
                    "kamers": rooms,
                }
            )
        except Exception as exc:
            log.warning("Pararius koop: failed to parse a listing: %s", exc)

    return houses


def scrape_funda_koop(page) -> list[dict[str, str]]:
    dump_html(page, "funda_koop")
    houses: list[dict[str, str]] = []

    addr_links = page.css('[data-testid="listingDetailsAddress"]')
    log.info("Funda koop: %d listing elements found", len(addr_links))

    for addr_link in addr_links:
        try:
            href = addr_link.attrib.get("href", "")
            if not href:
                continue
            url = make_absolute(href, "https://www.funda.nl")

            addr_spans = addr_link.css("span.truncate")
            address = " ".join((s.text or "").strip() for s in addr_spans).strip()

            city_el = addr_link.css(".text-neutral-80")
            city = (city_el[0].text or "").strip() if city_el else "Delft"

            if city and not is_delft_city(city):
                continue

            card = addr_link
            for _ in range(4):
                if card.parent:
                    card = card.parent

            price_el = card.css("div.font-semibold div.truncate")
            price = (price_el[0].text or "").strip() if price_el else ""

            area = ""
            rooms = ""
            feature_items = card.css("ul li span")
            for span in feature_items:
                txt = (span.text or "").strip()
                if "m²" in txt or "m2" in txt:
                    area = txt
                elif txt.isdigit():
                    # Funda's search card shows the bare number of *bedrooms*
                    # (slaapkamers) next to a bed icon, not total kamers.
                    # Cross-checked live: Meeslaan 18 shows "2" on Funda but is
                    # a 3-kamer flat on Realworks. Normalise to total kamers.
                    rooms = bedrooms_to_kamers(int(txt))

            if address:
                houses.append(
                    {
                        "url": url,
                        "straatnaamHuisnummer": address,
                        "plaats": city,
                        "vraagprijs": price,
                        "oppervlakte": area,
                        "kamers": rooms,
                    }
                )
        except Exception as exc:
            log.warning("Funda koop: failed to parse a listing: %s", exc)

    return houses


def _realworks_price(listing) -> str:
    """Extract the asking price from a Realworks list-card.

    Themes differ: Roepman/VW wrap it in `span.saleprice`; ZO/Hof van Delft/
    MORRIS surface it as the first `span.kenmerkValue`. Prefer the explicit
    sale-price span, then the euro-bearing kenmerkValue, then the first one.
    """
    sale = _first_text(listing, "span.saleprice")
    if sale:
        return sale
    kvs = listing.css("span.kenmerkValue")
    for kv in kvs:
        txt = (kv.text or "").strip()
        if "€" in txt:
            return txt
    return (kvs[0].text or "").strip() if kvs else ""


def _scrape_realworks_koop(page, base_url: str, site_name: str) -> list[dict[str, str]]:
    """Realworks-platform parser with an inverted status gate: keep koop URLs,
    skip /huur/ and /verkocht/. City check uses is_delft_city.

    Room count comes from the "Aantal kamers N" kenmerk (total kamers — the
    number follows the label, so the old `(\\d+)kamer` regex never matched it)
    and is stored as total kamers so the >= 2 gate is consistent with the other
    sources. List cards vary by theme: ZO/Hof van Delft use `.al2woning` with a
    full kenmerkValue list, MORRIS/VW use `.al4woning`, Roepman uses a bare
    `div.aanbodEntry` (address + saleprice only, no room count).
    """
    dump_html(page, site_name.lower().replace(" ", ""))
    houses: list[dict[str, str]] = []

    listings = _find_elements(
        page, ".al2woning", ".al4woning", "li.aanbodEntry", "div.aanbodEntry"
    )
    log.info("%s: %d listing elements found", site_name, len(listings))

    for listing in listings:
        try:
            link_els = listing.css("a.aanbodEntryLink")
            if not link_els:
                continue
            href = link_els[0].attrib.get("href", "")
            if not href:
                continue
            url = make_absolute(href, base_url)

            if "/huur/" in url:
                continue
            if "/verkocht/" in url:
                _record_sold_url(url.replace("/verkocht/", "/koop/"))
                continue

            address = _first_text(listing, "h3.street-address")
            city = _first_text(listing, "span.locality")

            if city and not is_delft_city(city):
                continue

            price = _realworks_price(listing)

            all_text = listing.get_all_text() or ""
            area = ""
            area_match = re.search(r"(\d+)\s*m[²2]", all_text)
            if area_match:
                area = f"{area_match.group(1)} m²"

            # "Aantal kamers 4" -> 4 total kamers. Deliberately anchored on the
            # "aantal kamers" label so it never picks up "aantal slaapkamers".
            rooms = ""
            rooms_match = re.search(
                r"aantal\s+kamers?\s*:?\s*(\d+)", all_text, re.IGNORECASE
            )
            if rooms_match:
                n = int(rooms_match.group(1))
                rooms = "1 kamer" if n == 1 else f"{n} kamers"

            houses.append(
                {
                    "url": url,
                    "straatnaamHuisnummer": address or "Onbekend",
                    "plaats": city or "Delft",
                    "vraagprijs": price,
                    "oppervlakte": area,
                    "kamers": rooms,
                }
            )
        except Exception as exc:
            log.warning("%s: failed to parse a listing: %s", site_name, exc)

    return houses


def scrape_zomakelaars_koop(page) -> list[dict[str, str]]:
    return _scrape_realworks_koop(
        page, "https://www.zomakelaars.nl", "ZO Makelaars koop"
    )


def _scrape_realworks_koop_http(
    feed_url: str, base_url: str, site_name: str, existing_urls: set[str]
) -> list[dict[str, str]]:
    """Fetch a Realworks koop list page over plain HTTP and parse it.

    Realworks serves the full listing DOM server-side, so a browser is
    unnecessary — and StealthyFetcher's rendered DOM reshuffles ZO Makelaars'
    cards into empty "Bewaar deze woning" widgets, which is why the browser
    path yielded 0. Plain HTTP returns clean, parseable HTML for every theme.
    """
    from scrapling.parser import Adaptor

    try:
        body = _http_get(feed_url)
    except Exception as exc:
        log.warning("%s: fetch failed: %s", site_name, exc)
        return []
    page = Adaptor(content=body, url=feed_url)
    return _scrape_realworks_koop(page, base_url, site_name)


# ---------------------------------------------------------------------------
# Realtime-listings JSON feed (Van Daal, Björnd)
# ---------------------------------------------------------------------------
# Same JSON shape the Bun app's RealtimeListingsJsonResponseProcessor consumes:
# each object exposes isSales/salesPrice/city/rooms/bedrooms/statusOrig. `rooms`
# is the *total* kamers count and `bedrooms` the slaapkamers — we store `rooms`
# so the shared >= 2 gate is consistent. Keep only available (not sold/bought)
# koop entries; price/city/junk are enforced later by passes_filters.


def _format_sales_price(sales_price: int) -> str:
    """1000-dotted euro string, e.g. 225000 -> "€ 225.000 k.k."."""
    return f"€ {sales_price:,.0f} k.k.".replace(",", ".")


def _scrape_realtime_listings_sales(
    feed_url: str, base_url: str, site_name: str
) -> list[dict[str, str]]:
    try:
        body = _http_get(feed_url)
    except Exception as exc:
        log.warning("%s: feed fetch failed: %s", site_name, exc)
        return []
    try:
        data = json.loads(body)
    except Exception as exc:
        log.warning("%s: feed JSON parse failed: %s", site_name, exc)
        return []
    if not isinstance(data, list):
        log.warning("%s: feed is not a list", site_name)
        return []

    houses: list[dict[str, str]] = []
    for entry in data:
        if not isinstance(entry, dict):
            continue
        if not entry.get("isSales"):
            continue
        if entry.get("statusOrig") != "available":
            sold_url = make_absolute(entry.get("url", ""), base_url)
            _record_sold_url(sold_url)
            continue

        sales_price = entry.get("salesPrice") or 0
        rooms = entry.get("rooms")
        rel_url = entry.get("url", "")
        houses.append(
            {
                "url": make_absolute(rel_url, base_url),
                "straatnaamHuisnummer": (entry.get("address") or "").strip(),
                "plaats": (entry.get("city") or "").strip(),
                "vraagprijs": (
                    _format_sales_price(int(sales_price)) if sales_price else ""
                ),
                "oppervlakte": (
                    f"{entry.get('livingSurface')} m²"
                    if entry.get("livingSurface")
                    else ""
                ),
                "kamers": f"{rooms} kamers" if rooms else "",
            }
        )
    log.info("%s: %d available koop entries in feed", site_name, len(houses))
    return houses


def scrape_vandaal_sales(existing_urls: set[str]) -> list[dict[str, str]]:
    return _scrape_realtime_listings_sales(
        VANDAAL_FEED_URL, VANDAAL_BASE, "Van Daal Makelaardij"
    )


def scrape_bjornd_sales(existing_urls: set[str]) -> list[dict[str, str]]:
    return _scrape_realtime_listings_sales(BJORND_FEED_URL, BJORND_BASE, "Björnd")


def scrape_roepman_sales(existing_urls: set[str]) -> list[dict[str, str]]:
    return _scrape_realworks_koop_http(
        ROEPMAN_KOOP_URL, "https://www.roepman.nl", "Roepman", existing_urls
    )


def scrape_morris_sales(existing_urls: set[str]) -> list[dict[str, str]]:
    return _scrape_realworks_koop_http(
        MORRIS_KOOP_URL,
        "https://www.morrismakelaardij.nl",
        "MORRIS Makelaardij",
        existing_urls,
    )


def scrape_hofvandelft_sales(existing_urls: set[str]) -> list[dict[str, str]]:
    return _scrape_realworks_koop_http(
        HOFVANDELFT_KOOP_URL,
        "https://www.hofvandelft.nl",
        "Hof van Delft",
        existing_urls,
    )


def scrape_zomakelaars_sales(existing_urls: set[str]) -> list[dict[str, str]]:
    return _scrape_realworks_koop_http(
        ZOMAKELAARS_KOOP_URL,
        "https://www.zomakelaars.nl",
        "ZO Makelaars koop",
        existing_urls,
    )


def scrape_vwmakelaars_sales(existing_urls: set[str]) -> list[dict[str, str]]:
    return _scrape_realworks_koop_http(
        VWMAKELAARS_KOOP_URL,
        "https://delft.vwmakelaars.nl",
        "VW Makelaars koop",
        existing_urls,
    )


# ---------------------------------------------------------------------------
# Prinsenstad Makelaardij koop via sale sitemap + per-listing plain HTTP
# ---------------------------------------------------------------------------
# Hayweb platform. The res-sale sitemap lists koop objects (many already sold);
# each detail page carries a "Vraagprijs" feature row and a "Status" we gate on.

_SOLD_STATUS_RE = re.compile(r"verkocht|onder bod|onder voorbehoud", re.IGNORECASE)


def _parse_prinsenstad_koop_listing(url: str, body: bytes) -> dict[str, str] | None:
    from scrapling.parser import Adaptor

    a = Adaptor(content=body, url=url)

    fields: dict[str, str] = {}
    for row in a.css("table.feautures tr"):
        label_els = row.css("td.object_detail_title")
        if not label_els:
            continue
        cells = row.css("td")
        if len(cells) < 2:
            continue
        label = (label_els[0].text or "").strip().lower()
        val = (cells[-1].text or "").strip() or (cells[-1].get_all_text() or "").strip()
        if label:
            fields.setdefault(label, val)

    price = fields.get("vraagprijs", "") or fields.get("koopprijs", "")
    if not price:
        return None

    status = fields.get("status", "")
    if _SOLD_STATUS_RE.search(status):
        _record_sold_url(url)
        return None

    h1_els = a.css("h1")
    h1 = (h1_els[0].get_all_text() or "").strip() if h1_els else ""
    if _SOLD_STATUS_RE.search(h1):
        _record_sold_url(url)
        return None
    header = re.sub(
        r"^(Te huur|Te koop|Verhuurd|Verkocht|Onder bod|Nieuw in verkoop)" r"\s*:\s*",
        "",
        h1,
        flags=re.I,
    ).strip()
    parts = [p.strip() for p in header.split(",") if p.strip()]
    address = parts[0] if parts else "Onbekend"
    city = ""
    if len(parts) >= 2:
        city = re.sub(r"^\d{4}\s*[A-Z]{2}\s*", "", parts[-1]).strip()

    if city and not is_delft_city(city):
        return None

    area = fields.get("woonoppervlakte", "")
    rooms_raw = fields.get("aantal kamers", "")
    # "4 (waarvan 3 slaapkamers)" -> total kamers 4.
    rooms_num = re.match(r"(\d+)", rooms_raw)
    if rooms_num:
        n = rooms_num.group(1)
        rooms = "1 kamer" if n == "1" else f"{n} kamers"
    else:
        rooms = rooms_raw

    return {
        "url": url,
        "straatnaamHuisnummer": address or "Onbekend",
        "plaats": city or "Delft",
        "vraagprijs": price,
        "oppervlakte": area,
        "kamers": rooms,
    }


def scrape_prinsenstad_sales(existing_urls: set[str]) -> list[dict[str, str]]:
    try:
        sitemap = _http_get(PRINSENSTAD_SALE_SITEMAP_URL)
    except Exception as exc:
        log.warning("Prinsenstad: sitemap fetch failed: %s", exc)
        return []
    try:
        root = ET.fromstring(sitemap)
    except ET.ParseError as exc:
        log.warning("Prinsenstad: sitemap parse failed: %s", exc)
        return []

    all_urls = [
        loc.text
        for u in root.findall("sm:url", _SITEMAP_NS)
        if (loc := u.find("sm:loc", _SITEMAP_NS)) is not None and loc.text
    ]
    log.info("Prinsenstad: sale sitemap has %d total URLs", len(all_urls))

    # Slugs encode the city ("…/koop/delft/…"); drop non-Delft before fetching.
    candidates = [u for u in all_urls if is_delft_city(u.replace("-", " "))]
    new_candidates = [u for u in candidates if u not in existing_urls]
    log.info(
        "Prinsenstad: %d Delft koop candidates, %d new",
        len(candidates),
        len(new_candidates),
    )

    existing_candidates = [u for u in candidates if u in existing_urls]
    for url in existing_candidates[:RECHECK_BATCH_SIZE]:
        try:
            body = _http_get(url)
        except Exception:
            continue
        try:
            _parse_prinsenstad_koop_listing(url, body)
        except Exception:
            continue

    houses: list[dict[str, str]] = []
    for url in new_candidates:
        try:
            body = _http_get(url)
        except Exception as exc:
            log.warning("Prinsenstad: detail fetch failed for %s: %s", url, exc)
            continue
        try:
            listing = _parse_prinsenstad_koop_listing(url, body)
        except Exception as exc:
            log.warning("Prinsenstad: parse failed for %s: %s", url, exc)
            continue
        if listing is not None:
            houses.append(listing)

    log.info("Prinsenstad: %d available koop match(es)", len(houses))
    return houses


# ---------------------------------------------------------------------------
# Olsthoorn Makelaars koop grid (custom WordPress "Sure" plugin)
# ---------------------------------------------------------------------------
# Every card on /wonen/{,page/N/} shows "&euro; ... k.k." (koop-only pricing;
# no huur listing was observed on any of the 20 sampled pages) plus a single
# status badge: reuse the Prinsenstad sold/onder-bod gate to skip Verkocht /
# Onder bod / Verkocht onder voorbehoud and keep Beschikbaar / Open huis.


def _parse_olsthoorn_card(card) -> dict[str, str] | None:
    href = card.attrib.get("href", "")
    if not href:
        return None
    url = make_absolute(href, OLSTHOORN_BASE)

    status = _first_text(card, ".card-house__status .card-house__label")
    if _SOLD_STATUS_RE.search(status):
        _record_sold_url(url)
        return None

    city = _first_text(card, "h2.card__title")
    if city and not is_delft_city(city):
        return None

    # The address (plain <p>) and price (<p><b>...) are the only two direct
    # <p> children of .short--info — distinguish by the <b> wrapper.
    address = ""
    price = ""
    for p in card.css(".short--info > p"):
        text = re.sub(r"\s+", " ", p.get_all_text() or "").strip()
        if p.css("b"):
            price = text
        else:
            address = text

    # Area/rooms icons wrap their number in a nested <sup> (e.g. "76 m<sup>2
    # </sup>"), which get_all_text() joins with a stray space ("76 m 2") — so
    # area is rebuilt from the leading digits instead of used verbatim.
    area = ""
    rooms = ""
    for data_div in card.css(".data--short .data"):
        icon = data_div.css("i")
        icon_class = icon[0].attrib.get("class", "") if icon else ""
        text = re.sub(r"\s+", " ", data_div.get_all_text() or "").strip()
        if "icon-sizes" in icon_class:
            m = re.match(r"(\d+)", text)
            area = f"{m.group(1)} m²" if m else text
        elif "icon-door" in icon_class:
            # Cross-checked against detail pages: the card's door-icon number
            # already is total kamers (e.g. card "5 kamers" == detail page's
            # "Kamers: 5 (waarvan 4 slaapkamers)"), so no bedrooms conversion.
            rooms = text

    return {
        "url": url,
        "straatnaamHuisnummer": address or "Onbekend",
        "plaats": city or "Delft",
        "vraagprijs": price,
        "oppervlakte": area,
        "kamers": rooms,
    }


def scrape_olsthoorn_sales(existing_urls: set[str]) -> list[dict[str, str]]:
    """Fetch every page of Olsthoorn Makelaars' /wonen/ koop grid over plain
    HTTP and return the Delft, available candidates.

    There's no reachable server-side filter, so pagination continues until a
    page returns zero cards at all (not zero Delft matches).
    """
    from scrapling.parser import Adaptor

    houses: list[dict[str, str]] = []
    max_pages = 25
    for page_num in range(1, max_pages + 1):
        url = (
            OLSTHOORN_WONEN_URL
            if page_num == 1
            else OLSTHOORN_WONEN_PAGE_URL.format(page=page_num)
        )
        try:
            body = _http_get(url)
        except Exception as exc:
            log.warning(
                "Olsthoorn Makelaars: fetch failed for page %d: %s", page_num, exc
            )
            break
        page = Adaptor(content=body, url=url)
        cards = page.css("a.card-house")
        if not cards:
            break
        for card in cards:
            try:
                house = _parse_olsthoorn_card(card)
            except Exception as exc:
                log.warning("Olsthoorn Makelaars: failed to parse a listing: %s", exc)
                continue
            if house is not None:
                houses.append(house)

    log.info("Olsthoorn Makelaars: %d Delft koop candidate(s) across pages", len(houses))
    return houses


# ---------------------------------------------------------------------------
# Van Silfhout Makelaars koop grid (WordPress + FacetWP REST refresh)
# ---------------------------------------------------------------------------
# FacetWP's JS builds a JSON POST to wp-json/facetwp/v1/refresh with the
# active facet selections; replicated here with facets pinned to koop+Delft.
# `first_load: 0` is required — with `first_load: 1` (the value the initial
# page load itself uses) the endpoint filters correctly but returns an empty
# `template` string. Each card's bare "Kamers" number is already the total
# kamers count (cross-checked against a detail page's separate "Slaapkamers"
# row), so no bedrooms conversion is needed.


def _facetwp_refresh(paged: int) -> dict:
    payload = {
        "action": "facetwp_refresh",
        "data": {
            "facets": {
                "status": ["te-koop"],
                "locaties": ["delft"],
                "aanbod": [],
                "aanbod_categorien": [],
                "koopprijs": [],
                "huurprijs": [],
                "oppervlakte": [],
                "perceel": [],
                "kamers": [],
            },
            "frozen_facets": {},
            "http_params": {"get": [], "uri": "woningaanbod", "url_vars": []},
            "template": "aanbod",
            "extras": {"sort": "default"},
            "soft_refresh": 0,
            "is_bfcache": 0,
            "first_load": 0,
            "paged": paged,
        },
    }
    req = urllib.request.Request(
        VANSILFHOUT_REFRESH_URL,
        data=json.dumps(payload).encode(),
        headers={"User-Agent": _PLAIN_HTTP_UA, "Content-Type": "application/json"},
    )
    with urllib.request.urlopen(req, timeout=30) as resp:
        return json.loads(resp.read())


def _parse_vansilfhout_card(card) -> dict[str, str] | None:
    link_els = card.css("a.straatnaamwoonplaats")
    if not link_els:
        return None
    href = link_els[0].attrib.get("href", "")
    if not href:
        return None
    url = make_absolute(href, VANSILFHOUT_BASE)

    address = _first_text(card, "h2.objecttitle")
    city = _first_text(card, "a.straatnaamwoonplaats span")
    if city and not is_delft_city(city):
        return None

    price = ""
    area = ""
    rooms = ""
    for li in card.css("ul.shortSpecs li"):
        text = re.sub(r"\s+", " ", li.get_all_text() or "").strip()
        label, _, value = text.partition(":")
        label = label.strip().lower()
        value = value.strip()
        if label == "vraagprijs":
            price = value
        elif label == "oppervlakte":
            m = re.search(r"(\d+)\s*m", value)
            area = f"{m.group(1)} m²" if m else value
        elif label == "kamers":
            m = re.match(r"(\d+)", value)
            if m:
                n = int(m.group(1))
                rooms = "1 kamer" if n == 1 else f"{n} kamers"

    return {
        "url": url,
        "straatnaamHuisnummer": address or "Onbekend",
        "plaats": city or "Delft",
        "vraagprijs": price,
        "oppervlakte": area,
        "kamers": rooms,
    }


# ---------------------------------------------------------------------------
# De Bruyn en Tak koop (same card structure as rental sidecar)
# ---------------------------------------------------------------------------


def scrape_debruynentak_sales(existing_urls: set[str]) -> list[dict[str, str]]:
    from scrapling.parser import Adaptor

    try:
        body = _http_get(DEBRUYNENTAK_KOOP_URL)
    except Exception as exc:
        log.warning("De Bruyn en Tak koop: fetch failed: %s", exc)
        return []
    page = Adaptor(content=body, url=DEBRUYNENTAK_KOOP_URL)
    houses: list[dict[str, str]] = []

    items = page.css("div.objectList div.item")
    log.info("De Bruyn en Tak koop: %d listing elements found", len(items))

    for item in items:
        try:
            link_el = item.css("a.itemTitel")
            if not link_el:
                continue
            href = link_el[0].attrib.get("href", "")
            if not href:
                continue
            url = make_absolute(href, "https://www.debruynentak.nl")

            label = _first_text(item, "div.label")
            if _SOLD_STATUS_RE.search(label):
                _record_sold_url(url)
                continue

            address = _first_text(item, "span.objectTitel")
            if not address:
                slug = href.rsplit("/", 1)[-1].removesuffix(".html")
                address = slug.replace("-", " ").strip().title()

            city = _first_text(item, "span.itemSubtitel") or "Delft"

            if city and not is_delft_city(city):
                continue

            price_txt = _first_text(item, "div.itemPrice .price")
            price = f"€ {price_txt}" if price_txt else ""

            specs = _first_text(item, "span.itemSpecs")

            area = ""
            area_match = re.search(r"(\d+)\s*m[²2]", specs)
            if area_match:
                area = f"{area_match.group(1)} m²"

            rooms = ""
            rooms_match = re.search(
                r"(\d+)\s*kamer", specs, re.IGNORECASE
            )
            if rooms_match:
                n = int(rooms_match.group(1))
                rooms = "1 kamer" if n == 1 else f"{n} kamers"

            houses.append(
                {
                    "url": url,
                    "straatnaamHuisnummer": address or "Onbekend",
                    "plaats": city,
                    "vraagprijs": price,
                    "oppervlakte": area,
                    "kamers": rooms,
                }
            )
        except Exception as exc:
            log.warning("De Bruyn en Tak koop: failed to parse a listing: %s", exc)

    log.info("De Bruyn en Tak koop: %d koop candidate(s)", len(houses))
    return houses


# ---------------------------------------------------------------------------
# Van Gulden Makelaardij koop (WordPress / Betheme, MTMO plugin)
# ---------------------------------------------------------------------------


def scrape_vangulden_sales(existing_urls: set[str]) -> list[dict[str, str]]:
    from scrapling.parser import Adaptor

    try:
        body = _http_get(VANGULDEN_KOOP_URL)
    except Exception as exc:
        log.warning("Van Gulden koop: fetch failed: %s", exc)
        return []
    page = Adaptor(content=body, url=VANGULDEN_KOOP_URL)
    houses: list[dict[str, str]] = []

    listings = page.css('a[href*="aanbod-detail"]')
    log.info("Van Gulden koop: %d listing elements found", len(listings))

    for listing in listings:
        try:
            href = listing.attrib.get("href", "")
            if not href:
                continue
            url = make_absolute(href, "https://vanguldenmakelaardij.nl")

            address = _first_text(listing, "div.titel")
            city = _first_text(listing, "p.notranslate")

            price = _first_text(listing, "div.price")

            if "per maand" in price.lower():
                continue

            if city and not is_delft_city(city):
                continue

            area = ""
            rooms = ""
            kenmerken = listing.css("div.kenmerk")
            for kenmerk in kenmerken:
                img_els = kenmerk.css("img")
                if not img_els:
                    continue
                alt = (img_els[0].attrib.get("alt", "") or "").lower()
                val = (kenmerk.get_all_text() or "").strip()
                if "woonoppervlakte" in alt:
                    area = val
                elif "kamers_icon" in alt:
                    rooms = val

            houses.append(
                {
                    "url": url,
                    "straatnaamHuisnummer": address or "Onbekend",
                    "plaats": city or "Delft",
                    "vraagprijs": price,
                    "oppervlakte": area,
                    "kamers": rooms,
                }
            )
        except Exception as exc:
            log.warning("Van Gulden koop: failed to parse a listing: %s", exc)

    log.info("Van Gulden koop: %d koop candidate(s)", len(houses))
    return houses


# ---------------------------------------------------------------------------
# Frisia Makelaars koop via sitemap + per-listing plain HTTP
# ---------------------------------------------------------------------------
# Same sitemap as the rental sidecar; the detail-page parser gates on a
# "Vraagprijs" feature block (the rental parser gates on "Huurprijs").


def _parse_frisia_koop_listing(url: str, body: bytes) -> dict[str, str] | None:
    from scrapling.parser import Adaptor

    a = Adaptor(content=body, url=url)

    price_block = next(
        (
            b
            for b in a.css(".panel__block__feature")
            if "Vraagprijs" in b.get_all_text(separator=" ", strip=True)
            or "Koopprijs" in b.get_all_text(separator=" ", strip=True)
        ),
        None,
    )
    if price_block is None:
        return None

    price_line = price_block.get_all_text(separator=" ", strip=True)
    for label in ("Vraagprijs", "Koopprijs"):
        if label in price_line:
            price = price_line.split(label, 1)[-1].strip(" |")
            break
    else:
        price = price_line

    status_block = next(
        (
            b
            for b in a.css(".panel__block__feature")
            if "Status" in b.get_all_text(separator=" ", strip=True)
        ),
        None,
    )
    if status_block is not None:
        status_text = status_block.get_all_text(separator=" ", strip=True)
        if _SOLD_STATUS_RE.search(status_text):
            _record_sold_url(url)
            return None

    h1 = a.css("h1")
    full_address = h1[0].get_all_text(separator=" ", strip=True) if h1 else ""
    parts = [p.strip() for p in full_address.split(",") if p.strip()]
    street = parts[0] if parts else "Onbekend"
    city = parts[-1] if len(parts) >= 2 else ""
    if city and not is_delft_city(city):
        return None

    area = ""
    rooms = ""
    for li in a.css(".section--intro__list li"):
        val = li.get_all_text(separator=" ", strip=True)
        if li.css(".icon-livearea"):
            area = val
        elif li.css(".icon-bedroom"):
            rooms = val

    return {
        "url": url,
        "straatnaamHuisnummer": street or "Onbekend",
        "plaats": city or "Delft",
        "vraagprijs": price,
        "oppervlakte": area,
        "kamers": rooms,
    }


def scrape_vansilfhout_sales(existing_urls: set[str]) -> list[dict[str, str]]:
    """Fetch Van Silfhout Makelaars' koop grid via FacetWP's REST refresh
    endpoint, with facets pinned to status=te-koop and locaties=delft.
    """
    from scrapling.parser import Adaptor

    houses: list[dict[str, str]] = []
    try:
        data = _facetwp_refresh(1)
    except Exception as exc:
        log.warning("Van Silfhout Makelaars: fetch failed: %s", exc)
        return []

    total_pages = data.get("settings", {}).get("pager", {}).get("total_pages", 1)

    for page_num in range(1, total_pages + 1):
        if page_num > 1:
            try:
                data = _facetwp_refresh(page_num)
            except Exception as exc:
                log.warning(
                    "Van Silfhout Makelaars: page %d fetch failed: %s", page_num, exc
                )
                break
        template = data.get("template", "")
        if not template:
            break
        page = Adaptor(
            content=template.encode(), url=f"{VANSILFHOUT_BASE}/woningaanbod/"
        )
        for card in page.css(".objectcontainer"):
            try:
                house = _parse_vansilfhout_card(card)
            except Exception as exc:
                log.warning(
                    "Van Silfhout Makelaars: failed to parse a listing: %s", exc
                )
                continue
            if house is not None:
                houses.append(house)

    log.info(
        "Van Silfhout Makelaars: %d Delft koop candidate(s) across pages", len(houses)
    )
    return houses


def scrape_frisia_sales(existing_urls: set[str]) -> list[dict[str, str]]:
    try:
        sitemap = _http_get(FRISIA_SITEMAP_URL)
    except Exception as exc:
        log.warning("Frisia koop: sitemap fetch failed: %s", exc)
        return []
    try:
        root = ET.fromstring(sitemap)
    except ET.ParseError as exc:
        log.warning("Frisia koop: sitemap parse failed: %s", exc)
        return []

    all_urls: list[str] = []
    for u in root.findall("sm:url", _SITEMAP_NS):
        loc = u.find("sm:loc", _SITEMAP_NS)
        if loc is not None and loc.text:
            all_urls.append(loc.text)
    log.info("Frisia koop: sitemap has %d total URLs", len(all_urls))

    candidates = [u for u in all_urls if is_delft_city(u.replace("-", " "))]
    new_candidates = [u for u in candidates if u not in existing_urls]
    log.info(
        "Frisia koop: %d Delft candidates, %d new",
        len(candidates),
        len(new_candidates),
    )

    existing_candidates = [u for u in candidates if u in existing_urls]
    for url in existing_candidates[:RECHECK_BATCH_SIZE]:
        try:
            body = _http_get(url)
        except Exception:
            continue
        try:
            _parse_frisia_koop_listing(url, body)
        except Exception:
            continue

    if len(new_candidates) > FRISIA_MAX_FETCHES_PER_CYCLE:
        log.info(
            "Frisia koop: capping detail fetches at %d this cycle",
            FRISIA_MAX_FETCHES_PER_CYCLE,
        )
        new_candidates = new_candidates[:FRISIA_MAX_FETCHES_PER_CYCLE]

    houses: list[dict[str, str]] = []
    for url in new_candidates:
        try:
            body = _http_get(url)
        except Exception as exc:
            log.warning("Frisia koop: detail fetch failed for %s: %s", url, exc)
            continue
        try:
            listing = _parse_frisia_koop_listing(url, body)
        except Exception as exc:
            log.warning("Frisia koop: parse failed for %s: %s", url, exc)
            continue
        if listing is not None:
            houses.append(listing)

    log.info("Frisia koop: %d koop match(es)", len(houses))
    return houses


# ---------------------------------------------------------------------------
# Marloes Makelaars koop via sitemap + per-listing plain HTTP
# ---------------------------------------------------------------------------
# Same WordPress sitemap as the rental sidecar. The rental parser keeps
# "per maand" prices; the sales parser keeps "k.k." / "v.o.n." prices.


def _parse_marloes_koop_listing(url: str, body: bytes) -> dict[str, str] | None:
    from scrapling.parser import Adaptor

    a = Adaptor(content=body, url=url)

    fields: dict[str, str] = {}
    for dt, dd in zip(a.css("dl dt"), a.css("dl dd")):
        label = (dt.text or "").strip().lower()
        if not label:
            continue
        val = (dd.text or "").strip() or (dd.get_all_text() or "").strip()
        fields[label] = val

    price = fields.get("prijs", "")
    price_lower = price.lower()
    if "per maand" in price_lower:
        return None
    if "k.k." not in price_lower and "v.o.n." not in price_lower:
        return None

    status = fields.get("status", "")
    if _SOLD_STATUS_RE.search(status):
        _record_sold_url(url)
        return None

    city = fields.get("plaats", "")
    if city and not is_delft_city(city):
        return None

    title_els = a.css("title")
    title = (title_els[0].text or "").strip() if title_els else ""
    address = title.split(" | ", 1)[0].strip()
    if city:
        address = re.sub(
            rf"\s+te\s+{re.escape(city)}\s*$", "", address, flags=re.I
        ).strip()

    rooms_raw = fields.get("slaapkamers", "")
    if rooms_raw.isdigit():
        rooms = bedrooms_to_kamers(int(rooms_raw))
    else:
        rooms = rooms_raw

    return {
        "url": url,
        "straatnaamHuisnummer": address or "Onbekend",
        "plaats": (city or "Delft").title(),
        "vraagprijs": price,
        "oppervlakte": fields.get("oppervlakte", ""),
        "kamers": rooms,
    }


def scrape_marloes_sales(existing_urls: set[str]) -> list[dict[str, str]]:
    try:
        sitemap = _http_get(MARLOES_SITEMAP_URL)
    except Exception as exc:
        log.warning("Marloes koop: sitemap fetch failed: %s", exc)
        return []
    try:
        root = ET.fromstring(sitemap)
    except ET.ParseError as exc:
        log.warning("Marloes koop: sitemap parse failed: %s", exc)
        return []

    all_urls: list[str] = []
    for u in root.findall("sm:url", _SITEMAP_NS):
        loc = u.find("sm:loc", _SITEMAP_NS)
        if loc is not None and loc.text:
            all_urls.append(loc.text)
    log.info("Marloes koop: sitemap has %d total URLs", len(all_urls))

    candidates = [u for u in all_urls if is_delft_city(u.replace("-", " "))]
    new_candidates = [u for u in candidates if u not in existing_urls]
    log.info(
        "Marloes koop: %d Delft candidates, %d new",
        len(candidates),
        len(new_candidates),
    )

    existing_candidates = [u for u in candidates if u in existing_urls]
    for url in existing_candidates[:RECHECK_BATCH_SIZE]:
        try:
            body = _http_get(url)
        except Exception:
            continue
        try:
            _parse_marloes_koop_listing(url, body)
        except Exception:
            continue

    houses: list[dict[str, str]] = []
    for url in new_candidates:
        try:
            body = _http_get(url)
        except Exception as exc:
            log.warning("Marloes koop: detail fetch failed for %s: %s", url, exc)
            continue
        try:
            listing = _parse_marloes_koop_listing(url, body)
        except Exception as exc:
            log.warning("Marloes koop: parse failed for %s: %s", url, exc)
            continue
        if listing is not None:
            houses.append(listing)

    log.info("Marloes koop: %d koop match(es)", len(houses))
    return houses


# ---------------------------------------------------------------------------
# PSG Wonen koop via sale sitemap + per-listing plain HTTP (Hayweb)
# ---------------------------------------------------------------------------
# Same Hayweb parser as Prinsenstad — reuses _parse_prinsenstad_koop_listing.


def scrape_psgwonen_sales(existing_urls: set[str]) -> list[dict[str, str]]:
    try:
        sitemap = _http_get(PSGWONEN_SALE_SITEMAP_URL)
    except Exception as exc:
        log.warning("PSG Wonen koop: sitemap fetch failed: %s", exc)
        return []
    try:
        root = ET.fromstring(sitemap)
    except ET.ParseError as exc:
        log.warning("PSG Wonen koop: sitemap parse failed: %s", exc)
        return []

    all_urls = [
        loc.text
        for u in root.findall("sm:url", _SITEMAP_NS)
        if (loc := u.find("sm:loc", _SITEMAP_NS)) is not None and loc.text
    ]
    log.info("PSG Wonen koop: sale sitemap has %d total URLs", len(all_urls))

    candidates = [u for u in all_urls if is_delft_city(u.replace("-", " "))]
    new_candidates = [u for u in candidates if u not in existing_urls]
    log.info(
        "PSG Wonen koop: %d Delft koop candidates, %d new",
        len(candidates),
        len(new_candidates),
    )

    existing_candidates = [u for u in candidates if u in existing_urls]
    for url in existing_candidates[:RECHECK_BATCH_SIZE]:
        try:
            body = _http_get(url)
        except Exception:
            continue
        try:
            _parse_prinsenstad_koop_listing(url, body)
        except Exception:
            continue

    houses: list[dict[str, str]] = []
    for url in new_candidates:
        try:
            body = _http_get(url)
        except Exception as exc:
            log.warning("PSG Wonen koop: detail fetch failed for %s: %s", url, exc)
            continue
        try:
            listing = _parse_prinsenstad_koop_listing(url, body)
        except Exception as exc:
            log.warning("PSG Wonen koop: parse failed for %s: %s", url, exc)
            continue
        if listing is not None:
            houses.append(listing)

    log.info("PSG Wonen koop: %d available koop match(es)", len(houses))
    return houses


# StealthyFetcher-backed sites (Cloudflare / heavy JS). Each entry is
# (name, url, parser); the parser receives a rendered page.
SITES = [
    ("Pararius koop", PARARIUS_KOOP_URL, scrape_pararius_koop),
    ("Funda koop", FUNDA_KOOP_URL, scrape_funda_koop),
]


# Sites that fetch themselves over plain HTTP (JSON feeds, Realworks list
# pages, sitemaps). Each entry is (name, fetcher) where
# fetcher(existing_urls) -> list[house].
CUSTOM_SITES = [
    ("Van Daal Makelaardij", scrape_vandaal_sales),
    ("Björnd", scrape_bjornd_sales),
    ("ZO Makelaars koop", scrape_zomakelaars_sales),
    ("VW Makelaars koop", scrape_vwmakelaars_sales),
    ("Roepman", scrape_roepman_sales),
    ("MORRIS Makelaardij", scrape_morris_sales),
    ("Hof van Delft", scrape_hofvandelft_sales),
    ("Prinsenstad Makelaardij", scrape_prinsenstad_sales),
    ("Olsthoorn Makelaars", scrape_olsthoorn_sales),
    ("Van Silfhout Makelaars", scrape_vansilfhout_sales),
    ("De Bruyn en Tak koop", scrape_debruynentak_sales),
    ("Van Gulden Makelaardij koop", scrape_vangulden_sales),
    ("Frisia Makelaars koop", scrape_frisia_sales),
    ("Marloes Makelaars koop", scrape_marloes_sales),
    ("PSG Wonen koop", scrape_psgwonen_sales),
]


# ---------------------------------------------------------------------------
# Fetch infrastructure (ported from python-sidecar/scraper.py)
# ---------------------------------------------------------------------------


def _is_browser_crash(exc: BaseException) -> bool:
    s = str(exc)
    return ("Page crashed" in s) or ("Target" in s and "closed" in s)


def _fetch_with_timeout(url: str) -> object:
    global _consecutive_fetch_failures
    from scrapling.fetchers import StealthyFetcher

    if _consecutive_fetch_failures > 0:
        probe = _fetch_pool.submit(lambda: None)
        try:
            probe.result(timeout=5)
        except TimeoutError:
            probe.cancel()
            _record_fetch_failure(url, "timeout (worker stuck)")
            raise TimeoutError(f"Pool worker stuck — skipping {url}")

    future = _fetch_pool.submit(
        StealthyFetcher.fetch,
        url,
        headless=True,
        solve_cloudflare=True,
        network_idle=True,
    )
    try:
        page = future.result(timeout=FETCH_TIMEOUT)
    except TimeoutError:
        _record_fetch_failure(url, "timeout")
        raise
    except Exception as exc:
        exc_str = str(exc)
        if "Page crashed" in exc_str:
            _record_fetch_failure(url, "page crashed")
        elif "Target" in exc_str and "closed" in exc_str:
            _record_fetch_failure(url, "target closed")
        raise
    _consecutive_fetch_failures = 0
    _failed_urls_in_streak.clear()
    return page


def _record_fetch_failure(url: str, reason: str) -> None:
    global _consecutive_fetch_failures
    if url in _failed_urls_in_streak:
        return
    _failed_urls_in_streak.add(url)
    _consecutive_fetch_failures += 1
    if _consecutive_fetch_failures >= MAX_CONSECUTIVE_FETCH_FAILURES:
        msg = (
            f"{_consecutive_fetch_failures} consecutive fetch failures "
            f"(last: {reason}). Browser likely wedged — exiting so Docker "
            f"restarts the container."
        )
        log.critical(msg)
        try:
            send_telegram_alert(f"♻️ <b>Sales scraper self-restart</b> — {msg}")
        except Exception:
            log.exception("Failed to send self-restart Telegram alert")
        try:
            SELF_RESTART_MARKER.parent.mkdir(parents=True, exist_ok=True)
            SELF_RESTART_MARKER.write_text(reason)
        except Exception:
            log.exception("Failed to write self-restart marker")
        os._exit(1)


def _scrape_paginated(name, url_template, parser, existing_urls):
    """Scrape all pages of a paginated site until the page is empty."""
    seen_urls: set[str] = set()
    houses: list[dict[str, str]] = []
    max_pages = 20
    empty_streak = 0

    for page_num in range(1, max_pages + 1):
        url = url_template.format(page=page_num)
        log.info("Fetching %s page %d ...", name, page_num)
        try:
            page = _fetch_with_timeout(url)
        except Exception as first_exc:
            if not (
                isinstance(first_exc, TimeoutError) or _is_browser_crash(first_exc)
            ):
                raise
            label = (
                "timed out" if isinstance(first_exc, TimeoutError) else "browser crash"
            )
            log.warning("%s page %d %s, retrying once ...", name, page_num, label)
            try:
                page = _fetch_with_timeout(url)
            except Exception as retry_exc:
                if not (
                    isinstance(retry_exc, TimeoutError) or _is_browser_crash(retry_exc)
                ):
                    raise
                log.warning(
                    "%s page %d failed twice (%s), skipping",
                    name,
                    page_num,
                    label,
                )
                send_throttled_timeout_alert(
                    f"{name}#page{page_num}",
                    f"⚠️ <b>{name}</b> page {page_num} failed ({label}) — " f"skipped",
                )
                break
        page_houses = parser(page)
        if not page_houses:
            break

        new_on_page = [
            h
            for h in page_houses
            if h["url"] not in existing_urls and h["url"] not in seen_urls
        ]
        for h in new_on_page:
            seen_urls.add(h["url"])
            houses.append(h)

        if new_on_page:
            empty_streak = 0
        else:
            empty_streak += 1
            if empty_streak >= 3:
                break

    return houses


# ---------------------------------------------------------------------------
# Persist + notify
# ---------------------------------------------------------------------------


def process_houses(
    conn, houses: list[dict[str, str]], existing_urls: set[str], seeding: bool
) -> int:
    """Insert previously-unknown houses; notify for genuinely new ones.

    - Skips URLs already present (restart-safe: no re-notify).
    - Seeding mode inserts without notifying (first-run backfill).
    - Cross-source dedup: a listing whose address+city already exists under a
      different URL is inserted but not announced again.
    Returns the number of Telegram notifications sent.
    """
    notified = 0
    for h in houses:
        if h["url"] in existing_urls:
            continue
        duplicate = find_duplicate(conn, h)
        save_house(conn, h)
        existing_urls.add(h["url"])
        if seeding:
            continue
        if duplicate is not None:
            log.info(
                "Skipping notify for %s — same address already seen at %s",
                h["url"],
                duplicate,
            )
            continue
        msg_ids = notify_new_listing(h)
        if msg_ids:
            conn.execute(
                "UPDATE sales SET tg_message_ids = ? WHERE url = ?",
                (json.dumps(msg_ids), h["url"]),
            )
            conn.commit()
        notified += 1
    return notified


def run_cycle():
    conn = init_db()
    existing_urls = get_existing_urls(conn)
    seeding = table_is_empty(conn)
    if seeding:
        log.info("Sales table empty — seeding without notifications")

    _cycle_sold_urls.clear()
    counts: dict[str, int] = {}
    notified_total = 0

    for name, url, parser in SITES:
        try:
            if "{page}" in url:
                houses = _scrape_paginated(name, url, parser, existing_urls)
            else:
                log.info("Fetching %s ...", name)
                try:
                    page = _fetch_with_timeout(url)
                except Exception as exc:
                    if not (isinstance(exc, TimeoutError) or _is_browser_crash(exc)):
                        raise
                    label = (
                        "timed out"
                        if isinstance(exc, TimeoutError)
                        else "browser crash"
                    )
                    log.warning("%s %s, retrying once ...", name, label)
                    page = _fetch_with_timeout(url)
                houses = parser(page)

            matched = [h for h in houses if passes_filters(h)]
            counts[name] = len(matched)
            log.info(
                "%s: %d scraped, %d match koop filters",
                name,
                len(houses),
                len(matched),
            )
            notified_total += process_houses(conn, matched, existing_urls, seeding)
        except TimeoutError:
            log.warning("%s timed out after %ds, skipping", name, FETCH_TIMEOUT)
            send_throttled_timeout_alert(
                name,
                f"⚠️ <b>{name}</b> timed out after {FETCH_TIMEOUT}s — skipped",
            )
        except Exception as exc:
            log.error("%s scrape failed: %s", name, exc, exc_info=True)

    for name, fetcher in CUSTOM_SITES:
        try:
            houses = fetcher(existing_urls)
            matched = [h for h in houses if passes_filters(h)]
            counts[name] = len(matched)
            log.info(
                "%s: %d scraped, %d match koop filters",
                name,
                len(houses),
                len(matched),
            )
            notified_total += process_houses(conn, matched, existing_urls, seeding)
        except Exception as exc:
            log.error("%s scrape failed: %s", name, exc, exc_info=True)

    sold_removed = process_sold_urls(conn, _cycle_sold_urls)
    recheck_removed = recheck_available_listings(conn)
    all_removed = sold_removed + recheck_removed
    if all_removed:
        log.info("Removed %d sold listing(s): %s", len(all_removed), all_removed)
        _send_sold_summary(all_removed)

    conn.close()
    return notified_total, counts


def main():
    if "--once" in sys.argv:
        notified, counts = run_cycle()
        log.info("Single cycle done — matches=%s notified=%d", counts, notified)
        return

    log.info(
        "Starting sales scraper (%d sources)  db=%s  interval=%ds  debug=%s",
        len(SITES) + len(CUSTOM_SITES),
        DB_PATH,
        CHECK_INTERVAL,
        DEBUG_DUMP,
    )

    if SELF_RESTART_MARKER.exists():
        try:
            reason = SELF_RESTART_MARKER.read_text().strip() or "unknown"
            send_telegram_alert(
                f"✅ <b>Sales scraper back online</b> — recovered after "
                f"self-restart (trigger: {reason})"
            )
        except Exception:
            log.exception("Failed to send self-restart recovery alert")
        finally:
            SELF_RESTART_MARKER.unlink(missing_ok=True)

    while True:
        try:
            notified, counts = run_cycle()
            log.info("Cycle done — matches=%s notified=%d", counts, notified)
        except Exception as exc:
            log.error("Cycle failed: %s", exc, exc_info=True)
        time.sleep(CHECK_INTERVAL)


if __name__ == "__main__":
    main()
