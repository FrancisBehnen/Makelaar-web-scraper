"""Standalone sales scraper: apartments FOR SALE (koop) in Delft.

Independent of the rental python-sidecar. Scrapes four koop sources, keeps
listings priced <= EUR 270.000 with >= 2 rooms in the city of Delft, stores
them in its own SQLite file and notifies a Telegram group directly. The rental
db.sqlite is never touched.

The parsers/fetch infrastructure are ported from python-sidecar/scraper.py. The
Telegram helpers (HTML escaping, status-button keyboard) and the listing
lifecycle (page-scoped gone/sold detection, round-robin recheck, replaceable
summary) are shared with the responder via the repo-root ``shared`` package;
only the ``urllib`` live sender is kept local for behaviour/test parity.
"""

import json
import logging
import os
import re
import sqlite3
import sys
import time
import urllib.error
import urllib.request
import xml.etree.ElementTree as ET
from concurrent.futures import ThreadPoolExecutor, TimeoutError
from pathlib import Path

from shared import lifecycle
from shared.tg import escape_html, status_button_row, status_keyboard

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

FETCH_TIMEOUT = int(os.environ.get("FETCH_TIMEOUT", "120"))
# Rechecks of sold status via StealthyFetcher (Funda/Pararius) need longer than
# the main scrape cycle: Funda's Cloudflare solve alone runs 60-120s and the
# 120s FETCH_TIMEOUT frequently times out, so sold Funda listings were never
# reliably detected. A separate, longer timeout is used only on the recheck path.
RECHECK_FETCH_TIMEOUT = int(os.environ.get("RECHECK_FETCH_TIMEOUT", "240"))
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
    r"bouwgrond|kavel|opslag|"
    r"bedrijfspand|bedrijfsruimte|bedrijfshal|"
    r"kantoorruimte|winkelruimte|praktijkruimte|horeca"
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
            status TEXT DEFAULT 'available',
            last_checked_at TEXT,
            sold_reason TEXT
        )
        """)
    cols = {row[1] for row in conn.execute("PRAGMA table_info(sales)")}
    if "tg_message_ids" not in cols:
        conn.execute("ALTER TABLE sales ADD COLUMN tg_message_ids TEXT DEFAULT ''")
    if "status" not in cols:
        conn.execute("ALTER TABLE sales ADD COLUMN status TEXT DEFAULT 'available'")
    # last_checked_at is the round-robin recheck cursor (ported from the
    # responder's delisting recheck; supersedes the old fixed rowid ordering).
    if "last_checked_at" not in cols:
        conn.execute("ALTER TABLE sales ADD COLUMN last_checked_at TEXT")
    # sold_reason records WHY a row went sold: 'explicit' (a positive sold/onder
    # bod status on the page or feed) vs 'gone' (a 404/410 or a mere
    # disappearance). Cross-source reconciliation trusts an *explicit* sold as
    # authoritative for the whole property (delete lagging siblings) but stays
    # conservative on a *gone* (a withdrawn duplicate may still be for sale
    # elsewhere). NULL on legacy rows sold before this migration.
    if "sold_reason" not in cols:
        conn.execute("ALTER TABLE sales ADD COLUMN sold_reason TEXT")
    # Small key/value store: persists the accumulating sold-summary state across
    # restarts / self-restarts (was an in-memory global before, which meant a
    # restart orphaned the live summary and started a new push).
    conn.execute("CREATE TABLE IF NOT EXISTS kv (key TEXT PRIMARY KEY, value TEXT)")
    conn.commit()
    return conn


def kv_get(conn, key: str) -> str | None:
    row = conn.execute("SELECT value FROM kv WHERE key = ?", (key,)).fetchone()
    return row[0] if row else None


def kv_set(conn, key: str, value: str) -> None:
    conn.execute(
        "INSERT OR REPLACE INTO kv (key, value) VALUES (?, ?)", (key, value)
    )
    conn.commit()


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


_PROPERTY_TYPE_PREFIX_RE = re.compile(
    r"^(appartement|woonhuis|eengezinswoning|studio|kamer|"
    r"bovenwoning|benedenwoning|tussenwoning|hoekwoning|"
    r"penthouse|maisonnette|herenhuis|villa)\s+",
    re.IGNORECASE,
)


def _norm_addr(text: str) -> str:
    t = _PROPERTY_TYPE_PREFIX_RE.sub("", (text or "").strip())
    return t.lower().replace(" ", "").replace("-", "").replace(".", "")


# Leading "NNNN XX" postcode (e.g. "2624 DK") that different sources prefix onto
# the city name with a DIFFERENT letter pair for the same neighbourhood.
_POSTCODE_PREFIX_RE = re.compile(r"^\s*\d{4}\s*[a-zA-Z]{2}\s*")


def _city_token(text: str) -> str:
    """Reduce a city field to a comparable token.

    Sources spell the same city differently: Funda emits "2624 DJ Delft" while
    Pararius emits "2624 DK Delft (Voorhof-Hoogbouw)". Comparing the raw strings
    (even with substring containment) treats these as different cities, so the
    same apartment gets notified twice. Stripping the leading postcode and any
    "(neighbourhood)" suffix collapses both to the bare "delft" token.
    """
    t = _POSTCODE_PREFIX_RE.sub("", text or "")
    t = re.sub(r"\(.*?\)", "", t)  # drop a "(neighbourhood)" suffix
    return t.strip().lower()


def find_duplicate(conn, h: dict[str, str]) -> str | None:
    """URL of an existing listing at the same address+city, else None.

    Mirrors the responder's find_prior_response: addresses are normalised by
    stripping spaces, hyphens, dots, and lowercasing; cities are reduced to a
    token (postcode + neighbourhood stripped) and matched on substring
    containment so "2624 DK Delft (Voorhof)" and "2624 DJ Delft" are equal.
    """
    target_addr = _norm_addr(h.get("straatnaamHuisnummer", ""))
    target_city = _city_token(h.get("plaats", ""))
    rows = conn.execute(
        "SELECT url, straatnaamHuisnummer, plaats FROM sales WHERE url != ?",
        (h["url"],),
    ).fetchall()
    for url, addr, plaats in rows:
        if _norm_addr(addr) != target_addr:
            continue
        city = _city_token(plaats)
        # Empty string is a substring of anything, mirroring SQL LIKE '%%'.
        if target_city in city or city in target_city:
            return url
    return None


# ---------------------------------------------------------------------------
# Telegram (ported from responder/tg.py; urllib instead of requests)
# ---------------------------------------------------------------------------


# Telegram HTML escaping and the status-button keyboard are shared with the
# responder (``shared.tg``); the responder is the bot's only getUpdates consumer
# and dispatches these callbacks statelessly, so the JSON must stay identical.
# ``escape_html`` is imported at module top; the button helpers are aliased under
# the local underscore names the rest of this module (and its tests) use.
_status_button_row = status_button_row
_status_keyboard = status_keyboard


def _send(
    chat_ids_raw: str,
    text: str,
    *,
    reply_markup: dict | None = None,
    disable_notification: bool = False,
) -> list[dict]:
    """Send a Telegram message and return [{"chat_id": ..., "message_id": ...}, ...].

    ``disable_notification`` sends the message silently (no push); used for the
    accumulating sold-summary so only genuinely new listings ping the group.
    """
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
        if disable_notification:
            payload["disable_notification"] = True
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
    except urllib.error.HTTPError as exc:
        # Surface Telegram's ``description`` (e.g. "message can't be deleted for
        # everyone" past the 48h window) — the bare HTTPError string only says
        # "400 Bad Request", which hid exactly this bug.
        try:
            detail = exc.read().decode("utf-8", "replace")
        except Exception:
            detail = ""
        log.warning(
            "Telegram delete %s/%s failed: %s %s", chat_id, message_id, exc, detail
        )
        return False
    except Exception as exc:
        log.warning("Telegram delete %s/%s failed: %s", chat_id, message_id, exc)
        return False


def _sold_marker_text(addr: str, url: str, reason: str) -> str:
    """In-place replacement text for a sold listing whose original notification
    can't be deleted.

    Telegram only lets a bot delete its own messages for 48 hours; a listing
    that sells has almost always been on the market longer than that, so the
    delete fails and the live card would otherwise linger. ``editMessageText``
    has no such time limit, so we edit the card to mark it sold instead — the
    group never shows a stale "available" listing.
    """
    label = "Niet meer beschikbaar" if reason == "gone" else "Verkocht / onder bod"
    return (
        f"\U0001f6d1 <b>{escape_html(label)}</b>\n"
        f"<s>{escape_html(addr or url)}</s>\n"
        f"{escape_html(url)}"
    )


def _edit_message(chat_id: str, message_id: int, text: str) -> bool:
    """Edit a previously-sent message in place (silent — no push).

    Returns False on any Telegram error (e.g. "message to edit not found" once
    the message is deleted, or past the edit window) so the caller can fall back
    to sending a fresh summary."""
    if not TELEGRAM_BOT_TOKEN:
        return False
    api_url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/editMessageText"
    body = json.dumps(
        {
            "chat_id": chat_id,
            "message_id": message_id,
            "text": text,
            "parse_mode": "HTML",
            "disable_web_page_preview": True,
        }
    ).encode()
    req = urllib.request.Request(
        api_url, data=body, headers={"Content-Type": "application/json"}
    )
    try:
        resp_body = urllib.request.urlopen(req, timeout=10).read()
        resp = json.loads(resp_body)
        return resp.get("ok", False)
    except Exception as exc:
        log.warning("Telegram edit %s/%s failed: %s", chat_id, message_id, exc)
        return False


def _edit_messages(message_ids: list[dict], text: str) -> bool:
    """Edit every {chat_id, message_id} in the summary state. True if all edits
    succeeded (a partial/total failure triggers the send-fresh fallback)."""
    if not message_ids:
        return False
    return all(
        _edit_message(str(entry["chat_id"]), entry["message_id"], text)
        for entry in message_ids
    )


def _delete_listing_messages(
    conn, url: str, reason: str = "explicit"
) -> tuple[str, str] | None:
    """Delete Telegram messages for a listing and mark it as sold.

    ``reason`` records WHY the row went sold ('explicit' vs 'gone'); it is
    persisted alongside the status so later cross-source reconciliation can tell
    an authoritative sold from an ambiguous disappearance. Returns an
    ``(address, url)`` pair if the listing was transitioned, else None.
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
        marker = _sold_marker_text(addr, url, reason)
        for entry in json.loads(tg_ids_raw):
            cid, mid = str(entry["chat_id"]), entry["message_id"]
            if not _delete_message(cid, mid):
                # Delete failed (almost always Telegram's 48h limit on a bot
                # deleting its own messages). Fall back to editing the card in
                # place so it reads "sold" rather than lingering as a live
                # listing above the summary.
                _edit_message(cid, mid, marker)

    conn.execute(
        "UPDATE sales SET status = 'sold', sold_reason = ? WHERE url = ?",
        (reason, url),
    )
    conn.commit()

    log.info("Marked sold and deleted TG message(s): %s (%s)", addr, url)
    return addr or url, url


def _sold_siblings(
    conn, url: str, *, primary_reason: str
) -> list[tuple[str, str]]:
    """Propagate a sold transition to same-address listings from OTHER sources.

    The same apartment is often listed on several sites under different URLs
    (cross-source dedup notifies once but stores every row). How we propagate to
    the sibling rows depends on WHY the primary went sold:

    * ``primary_reason == 'explicit'`` — a positive sold/onder-bod status was
      asserted (page phrase/badge, feed statusOrig, or a Realworks /verkocht/
      URL). That is authoritative for the whole PHYSICAL property: lagging
      portals still showing it available simply haven't caught up yet, so their
      stale cards are deleted UNCONDITIONALLY (no per-sibling page re-check —
      that lag is exactly what we're fixing).
    * ``primary_reason == 'gone'`` — the primary merely disappeared (404/410 or
      dropped out of a feed with no sold status). That is ambiguous: a makelaar
      may have withdrawn a duplicate listing while the flat is still for sale
      elsewhere (the Westvest case). So we stay conservative and delete a
      sibling only if its OWN page also reads sold (``_sold_reason``).

    Returns the ``(address, url)`` pairs actually transitioned.
    """
    row = conn.execute(
        "SELECT straatnaamHuisnummer, plaats FROM sales WHERE url = ?", (url,)
    ).fetchone()
    if row is None:
        return []
    target_addr = _norm_addr(row[0])
    target_city = _city_token(row[1])
    candidates = conn.execute(
        "SELECT url, straatnaamHuisnummer, plaats FROM sales "
        "WHERE status = 'available' AND url != ?",
        (url,),
    ).fetchall()
    removed: list[tuple[str, str]] = []
    for sib_url, sib_addr, sib_plaats in candidates:
        if _norm_addr(sib_addr) != target_addr:
            continue
        sib_city = _city_token(sib_plaats)
        if not (target_city in sib_city or sib_city in target_city):
            continue
        if primary_reason == "explicit":
            # Property is sold -> delete the lagging sibling regardless of what
            # its own (stale) page still shows.
            entry = _delete_listing_messages(conn, sib_url, "explicit")
            if entry is not None:
                removed.append(entry)
            continue
        # Conservative (gone) branch: only propagate if the sibling itself reads
        # sold, recording its own computed reason.
        try:
            sib_reason = _sold_reason(sib_url)
        except Exception as exc:
            log.warning("Sibling sold-check of %s failed: %s", sib_url, exc)
            continue
        if sib_reason is None:
            continue  # sibling still live on its own site — leave it be
        entry = _delete_listing_messages(conn, sib_url, sib_reason)
        if entry is not None:
            removed.append(entry)
    return removed


def process_sold_urls(conn, sold_urls: set[str]) -> list[tuple[str, str]]:
    """Check sold URLs against DB and delete Telegram messages for matches.

    These are all in-cycle *explicit* sold detections (feed statusOrig sold,
    Realworks /verkocht/, card badges), so propagation to same-address siblings
    is authoritative. Returns the ``(address, url)`` pairs transitioned to sold.
    """
    removed: list[tuple[str, str]] = []
    for url in sold_urls:
        entry = _delete_listing_messages(conn, url, "explicit")
        if entry is not None:
            removed.append(entry)
            removed.extend(_sold_siblings(conn, url, primary_reason="explicit"))
    return removed


# Page-scoped sold detection for the universal recheck (ported from the
# responder's delisting logic). Sidebar/footer carousels are stripped first;
# unambiguous "this listing" phrases are trusted anywhere, bare status badges
# ("verkocht", "onder bod", …) only inside the page's header region — so a
# neighbouring "recent verkocht" card can't wrongly delete a live listing.
_SOLD_PAGE_STATUS_RE = re.compile(
    r"deze woning is verkocht|status:\s*verkocht",
    re.IGNORECASE,
)
_SOLD_BADGE_STATUS_RE = re.compile(
    r"verkocht|onder bod|onder voorbehoud",
    re.IGNORECASE,
)


_STEALTHY_RECHECK_DOMAINS = ("funda.nl", "pararius.nl")


def _needs_stealthy_recheck(url: str) -> bool:
    return any(d in url for d in _STEALTHY_RECHECK_DOMAINS)


def _stealthy_fetch(url: str) -> bytes:
    """Fetch a single page via StealthyFetcher (headless browser).

    Bypasses the consecutive-failure counter used by the scrape cycle so a
    recheck timeout doesn't trigger a self-restart. Raises HTTPError on
    404/410 so ``lifecycle.is_gone`` treats them as gone.
    """
    from scrapling.fetchers import StealthyFetcher

    future = _fetch_pool.submit(
        StealthyFetcher.fetch,
        url,
        headless=True,
        solve_cloudflare=True,
        network_idle=True,
    )
    # Rechecks get the longer RECHECK_FETCH_TIMEOUT (Funda's Cloudflare solve
    # regularly overruns the 120s scrape-cycle timeout).
    page = future.result(timeout=RECHECK_FETCH_TIMEOUT)
    status = getattr(page, "status", 200)
    if status in (404, 410):
        raise urllib.error.HTTPError(url, status, "Gone", {}, None)
    return page.body


def _sold_reason(url: str) -> str | None:
    """Classify a listing page as ``'explicit'`` / ``'gone'`` / ``None``.

    Fetches the page via the same StealthyFetcher/plain-HTTP routing the recheck
    uses, then distinguishes the two kinds of "sold" that the reconciliation
    policy treats differently:

    * ``'explicit'`` — the page reads sold (an unambiguous status phrase or a
      status badge in the header region, via ``lifecycle.reads_gone``). This is
      a positive assertion the property is under contract.
    * ``'gone'`` — the page 404/410s (the listing was pulled). Ambiguous on its
      own: the flat may still be for sale on another portal.
    * ``None`` — the page is still live.

    Non-HTTP fetch errors propagate so the caller can skip without deciding.
    """
    fetch = (
        _stealthy_fetch
        if _needs_stealthy_recheck(url)
        else lambda u: _http_get(u, timeout=15)
    )
    try:
        body = fetch(url)
    except urllib.error.HTTPError as exc:
        return "gone" if exc.code in lifecycle.DEFAULT_GONE_HTTP_CODES else None
    if lifecycle.reads_gone(
        body.decode("utf-8", errors="ignore"),
        page_status_re=_SOLD_PAGE_STATUS_RE,
        badge_status_re=_SOLD_BADGE_STATUS_RE,
    ):
        return "explicit"
    return None


def _is_sold(url: str) -> bool:
    """Page-scoped sold check for one listing URL (404/410 also counts).

    Thin bool wrapper over :func:`_sold_reason` for callers that don't care WHY.
    """
    return _sold_reason(url) is not None


def _touch_checked(conn, url: str) -> None:
    conn.execute(
        "UPDATE sales SET last_checked_at = datetime('now') WHERE url = ?",
        (url,),
    )
    conn.commit()


def recheck_available_listings(conn) -> list[tuple[str, str]]:
    """Re-fetch a batch of available listings via plain HTTP and check status.

    Universal fallback for sources where sold detection doesn't happen during
    the normal scrape (sitemap detail pages, Funda, etc.). Uses the responder's
    round-robin cursor (least-recently-checked first, cursor advanced before the
    fetch) and its page-scoped detection. Returns ``(address, url)`` pairs
    transitioned to sold.
    """
    rows = conn.execute(
        "SELECT url FROM sales WHERE status = 'available' "
        "ORDER BY last_checked_at ASC LIMIT ?",
        (RECHECK_BATCH_SIZE,),
    ).fetchall()
    # Remember the reason each primary went sold so sibling propagation can be
    # reason-aware (explicit -> delete lagging siblings; gone -> conservative).
    reasons: dict[str, str] = {}

    def _gone(row):
        reason = _sold_reason(row[0])
        if reason is not None:
            reasons[row[0]] = reason
            return True
        return False

    removed = lifecycle.run_recheck(
        rows,
        mark_checked=lambda row: _touch_checked(conn, row[0]),
        gone=_gone,
        on_gone=lambda row: _delete_listing_messages(
            conn, row[0], reasons.get(row[0], "explicit")
        ),
        # Surface (don't swallow) recheck fetch/parse errors so a Funda timeout
        # is visible in the logs instead of silently advancing the cursor.
        on_error=lambda row, exc: log.warning(
            "Recheck of %s failed: %s", row[0], exc
        ),
    )
    # Propagate each primary sold to same-address siblings on other sources,
    # passing the reason so an explicit sold cleans up lagging siblings while a
    # gone stays conservative.
    extra: list[tuple[str, str]] = []
    for _addr, sold_url in removed:
        extra.extend(
            _sold_siblings(
                conn, sold_url, primary_reason=reasons.get(sold_url, "explicit")
            )
        )
    return removed + extra


def reconcile_cross_source(conn) -> list[tuple[str, str]]:
    """Clean up existing cross-source lag: available rows whose sibling is sold.

    ``recheck_available_listings`` / ``process_sold_urls`` only propagate at the
    MOMENT a row goes sold. This per-cycle pass catches the standing state where
    an available row A already has a same-address (+ city-token) sibling S that
    is sold — e.g. Funda sold last cycle but Pararius still lingers. The policy
    mirrors ``_sold_siblings``: an *explicit* sold on S is authoritative for the
    whole property (delete A), a *gone* on S is not (leave A for the normal
    per-page recheck).

    Legacy rows sold before the ``sold_reason`` migration carry NULL; for those
    we do ONE bounded re-fetch of S's page (``_sold_reason``) to backfill the
    reason and decide — capped at ``RECHECK_BATCH_SIZE`` per cycle so a backlog
    of legacy siblings can't trigger a burst of StealthyFetch calls.

    Returns the ``(address, url)`` pairs transitioned to sold.
    """
    available = conn.execute(
        "SELECT url, straatnaamHuisnummer, plaats FROM sales "
        "WHERE status = 'available'"
    ).fetchall()
    # Snapshot the sold rows once; rows we mark sold below aren't re-considered
    # as siblings this cycle (cascades are picked up next cycle — keeps bounded).
    sold_rows = conn.execute(
        "SELECT url, straatnaamHuisnummer, plaats, sold_reason FROM sales "
        "WHERE status = 'sold'"
    ).fetchall()

    removed: list[tuple[str, str]] = []
    # One shared budget for every legacy re-fetch this cycle (both the sibling S
    # and the conservative A fallback fetch count), so a backlog of legacy rows
    # can't fan out into a burst of StealthyFetch calls.
    refetch_budget = RECHECK_BATCH_SIZE
    for a_url, a_addr, a_plaats in available:
        a_norm = _norm_addr(a_addr)
        a_city = _city_token(a_plaats)
        match = None
        for s_url, s_addr, s_plaats, s_reason in sold_rows:
            if _norm_addr(s_addr) != a_norm:
                continue
            s_city = _city_token(s_plaats)
            if a_city in s_city or s_city in a_city:
                match = (s_url, s_reason)
                break
        if match is None:
            continue
        s_url, s_reason = match

        if s_reason == "explicit":
            entry = _delete_listing_messages(conn, a_url, "explicit")
            if entry is not None:
                removed.append(entry)
        elif s_reason == "gone":
            # Ambiguous sold — do not propagate; the per-page recheck handles A.
            continue
        else:
            # Legacy NULL reason: one bounded re-fetch of S decides.
            if refetch_budget <= 0:
                continue
            refetch_budget -= 1
            try:
                backfilled = _sold_reason(s_url)
            except Exception as exc:
                log.warning("Reconcile re-fetch of %s failed: %s", s_url, exc)
                continue
            if backfilled is None:
                # S no longer even reads sold — can't decide; leave for a later
                # cycle rather than guess.
                continue
            # Persist the decision so this expensive re-fetch isn't repeated.
            conn.execute(
                "UPDATE sales SET sold_reason = ? WHERE url = ?",
                (backfilled, s_url),
            )
            conn.commit()
            if backfilled == "explicit":
                entry = _delete_listing_messages(conn, a_url, "explicit")
                if entry is not None:
                    removed.append(entry)
            elif refetch_budget > 0:
                # S is 'gone' — conservative: only delete A if A's own page
                # reads sold too (counts against the same re-fetch budget).
                refetch_budget -= 1
                try:
                    a_reason = _sold_reason(a_url)
                except Exception as exc:
                    log.warning("Reconcile re-fetch of %s failed: %s", a_url, exc)
                    continue
                if a_reason is not None:
                    entry = _delete_listing_messages(conn, a_url, a_reason)
                    if entry is not None:
                        removed.append(entry)
    return removed


_SOLD_SUMMARY_KV = "sold_summary_state"
_SOLD_SUMMARY_TITLE = (
    "\U0001f6d1 <b>{count} {word} verkocht/onder bod — bericht(en) opgeruimd</b>"
)


def _send_sold_summary(conn, listings: list[tuple[str, str]]) -> None:
    """Update the persistent, accumulating sold-summary (edited in place).

    ``listings`` is a list of ``(address, url)`` pairs; each bullet links the
    address to its listing URL. The summary is a single message per local day
    that is edited (silently) as more listings sell, so only genuinely new
    listings ever push. State survives restarts via the ``kv`` table.
    """

    def build_text(entries: list[tuple[str, str]]) -> str:
        return lifecycle.build_summary_text(
            entries, title_template=_SOLD_SUMMARY_TITLE, escape=escape_html
        )

    def load_state() -> dict | None:
        raw = kv_get(conn, _SOLD_SUMMARY_KV)
        return json.loads(raw) if raw else None

    lifecycle.upsert_accumulating_summary(
        listings,
        load_state=load_state,
        save_state=lambda state: kv_set(conn, _SOLD_SUMMARY_KV, json.dumps(state)),
        edit=_edit_messages,
        # Silent send: the summary must never push (only new listings do).
        send=lambda text: _send(
            TELEGRAM_SALES_CHAT_IDS, text, disable_notification=True
        ),
        delete=lambda message_ids: [
            _delete_message(str(e["chat_id"]), e["message_id"]) for e in message_ids
        ],
        build_text=build_text,
    )


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
    # Reconcile standing cross-source lag (available rows whose sibling already
    # went sold in an earlier cycle). Folded into the same batched summary.
    reconcile_removed = reconcile_cross_source(conn)
    all_removed = sold_removed + recheck_removed + reconcile_removed
    if all_removed:
        log.info("Removed %d sold listing(s): %s", len(all_removed), all_removed)
        _send_sold_summary(conn, all_removed)

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
