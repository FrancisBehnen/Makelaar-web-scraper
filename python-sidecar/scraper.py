import json
import logging
import os
import re
import sqlite3
import time
import urllib.request
import xml.etree.ElementTree as ET
from concurrent.futures import ThreadPoolExecutor, TimeoutError
from pathlib import Path

from scrapling.fetchers import StealthyFetcher
from scrapling.parser import Adaptor

FETCH_TIMEOUT = int(os.environ.get("FETCH_TIMEOUT", "120"))
MAX_CONSECUTIVE_FETCH_FAILURES = int(
    os.environ.get("MAX_CONSECUTIVE_FETCH_FAILURES", "3")
)
TIMEOUT_ALERT_THROTTLE_SECONDS = int(
    os.environ.get("TIMEOUT_ALERT_THROTTLE_SECONDS", str(6 * 3600))
)
_fetch_pool = ThreadPoolExecutor(max_workers=1)
_consecutive_fetch_failures = 0
# Distinct URLs that have failed since the last successful fetch. A single sick
# site that keeps timing out is not a wedged browser, so the global counter
# only ticks for URLs we haven't already counted in this streak.
_failed_urls_in_streak: set[str] = set()
_last_timeout_alert: dict[str, float] = {}

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
)
log = logging.getLogger("sidecar")

DB_PATH = os.environ.get("DB_PATH", "data/db.sqlite")
# Dropped on the data volume just before a self-restart exit; its presence on
# the next boot means the previous process self-restarted.
SELF_RESTART_MARKER = Path(DB_PATH).parent / ".self_restart"
CHECK_INTERVAL = int(os.environ.get("CHECK_INTERVAL", "300"))
DEBUG_DUMP = os.environ.get("DEBUG_DUMP", "").lower() in ("1", "true", "yes")
TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_IDS = os.environ.get("TELEGRAM_CHAT_IDS", "")
# Operational alerts (timeouts, self-restarts) go here instead of the group
# chat. Falls back to TELEGRAM_CHAT_IDS when unset.
TELEGRAM_ALERT_CHAT_IDS = os.environ.get("TELEGRAM_ALERT_CHAT_IDS", "")

# ---------------------------------------------------------------------------
# Site URLs
# ---------------------------------------------------------------------------

PARARIUS_URL = "https://www.pararius.nl/huurwoningen/delft/0-1500/page-{page}"
FUNDA_URL = (
    "https://www.funda.nl/zoeken/huur"
    "?selected_area=%5B%22delft%22%5D&price=%220-1500%22"
)
VBT_URL = "https://vbtverhuurmakelaars.nl/woningen?city=delft&maxPrice=1500"
# Marloes' /aanbod/huur/ index is browser-render-friendly but the listing CPT
# sitemap covers every property — fetched directly we skip the JS path.
MARLOES_SITEMAP_URL = (
    "https://www.marloesmakelaars.nl/wp-sitemap-posts-property-1.xml"
)
HOFVANDELFT_URL = (
    "https://www.hofvandelft.nl/aanbod/woningaanbod/DELFT/+5km/-1500/huur/"
)
EENTWEEDRIEWONEN_URL = "https://www.123wonen.nl/huurwoningen/in/delft"
ROTSVAST_URL = (
    "https://www.rotsvast.nl/huren/?search=Delft&radius=5&price_to=1500"
)
PRINSENSTAD_URL = (
    "https://prinsenstadmakelaardij.nl/woningaanbod/huur"
    "?availability=1&pricerange.maxprice=1500"
)
PACTUM_URL = "https://www.pactumvastgoed.nl/huurwoningen"
VWMAKELAARS_URL = "https://delft.vwmakelaars.nl/aanbod/woningaanbod/huur/"
RENTAROOM_URL = "https://rent-a-room-delft.nl/grid-default/"
# Frisia's WAF refuses Camoufox at the TCP layer on the listings index. The
# sitemap and individual property pages still serve over plain HTTP, so we
# walk the sitemap instead of rendering the search page.
FRISIA_SITEMAP_URL = "https://frisiamakelaars.nl/sitemap/properties.xml"
FRISIA_MAX_FETCHES_PER_CYCLE = int(
    os.environ.get("FRISIA_MAX_FETCHES_PER_CYCLE", "10")
)
OUDEDELFT_URL = "https://oudedelft.com/huur-2/"
PSGWONEN_URL = "https://www.psg-wonen.nl/woningaanbod/huur"
VANGULDEN_URL = "https://vanguldenmakelaardij.nl/huuraanbod/"
ZOMAKELAARS_URL = (
    "https://www.zomakelaars.nl/aanbod/woningaanbod/vestiging-906351/huur/"
)
IKWILHUREN_URL = "https://ikwilhuren.nu/aanbod/delft/"
ZEVENTIGWONEN_URL = "https://www.070wonen.nl/huurwoningen/"
DEBRUYNENTAK_URL = "https://www.debruynentak.nl/aanbod/woningen/te-huur/delft/"
# National landlord — its ?location= filter takes a single city, so we fetch
# the full overview and narrow to the Delft area with is_delft_area/MAX_PRICE.
NATIONAALGRONDBEZIT_URL = "https://www.nationaalgrondbezit.nl/huuraanbod"

# ---------------------------------------------------------------------------
# Filtering criteria (matches the Bun app's RealtimeListingsJsonResponseProcessor)
# ---------------------------------------------------------------------------

DELFT_AREA_CITIES = {
    "delft", "delfgauw", "den hoorn", "rijswijk",
    "schipluiden", "nootdorp", "pijnacker",
}
MAX_PRICE = 1500


def parse_price_euros(text: str) -> int | None:
    cleaned = re.sub(r"[^\d]", "", text.replace(".", ""))
    return int(cleaned) if cleaned else None


def is_delft_area(text: str) -> bool:
    """True when *text* mentions one of the target cities.

    Accepts a bare city name or a longer string (e.g. a Pararius sub-title
    like "2611 AB Delft (Binnenstad)") so every parser can share one filter.
    """
    lowered = (text or "").lower()
    return any(
        re.search(rf"\b{re.escape(city)}\b", lowered)
        for city in DELFT_AREA_CITIES
    )


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


# Plain-HTTP user agent for sites that refuse Camoufox at the TCP layer but
# answer ordinary HTTP. Used by the sitemap-based fetchers.
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
# Database (matches the Bun app's schema exactly)
# ---------------------------------------------------------------------------

def init_db():
    Path(DB_PATH).parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(DB_PATH)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS houses (
            url TEXT PRIMARY KEY,
            straatnaamHuisnummer TEXT,
            plaats TEXT,
            vraagprijs TEXT,
            oppervlakte TEXT,
            kamers TEXT
        )
        """
    )
    conn.commit()
    return conn


def get_existing_urls(conn):
    return {row[0] for row in conn.execute("SELECT url FROM houses")}


def save_houses(conn, houses):
    conn.executemany(
        """
        INSERT OR REPLACE INTO houses
            (url, straatnaamHuisnummer, plaats, vraagprijs, oppervlakte, kamers)
        VALUES (?, ?, ?, ?, ?, ?)
        """,
        [
            (
                h["url"],
                h["straatnaamHuisnummer"],
                h["plaats"],
                h["vraagprijs"],
                h["oppervlakte"],
                h["kamers"],
            )
            for h in houses
        ],
    )
    conn.commit()


# ---------------------------------------------------------------------------
# Telegram (matches the Bun app's message format)
# ---------------------------------------------------------------------------

def send_telegram(houses):
    """Notify all chats about each house. Returns the houses that were
    delivered to every chat — only those should be persisted, so a failed
    send is retried on the next cycle instead of being silently lost."""
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_IDS:
        log.warning("Telegram not configured, skipping notifications")
        return houses

    chat_ids = [c.strip() for c in TELEGRAM_CHAT_IDS.split(",") if c.strip()]
    api_url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"

    delivered = []
    for house in houses:
        text = (
            "\U0001f6a8 <b>Nieuw huis gevonden!</b> \U0001f6a8\n\n"
            "<blockquote>Gegevens van het huis:\n"
            f"Adres: {house['straatnaamHuisnummer']}, {house['plaats']}\n"
            f"Plaats: {house['plaats']}\n"
            f"Vraagprijs: {house['vraagprijs']}\n"
            f"Oppervlakte: {house['oppervlakte']}\n"
            f"Kamers: {house['kamers']}\n"
            f"URL: {house['url']}"
            "</blockquote>"
        )
        ok = True
        for chat_id in chat_ids:
            body = json.dumps(
                {"chat_id": chat_id, "text": text, "parse_mode": "HTML"}
            ).encode()
            req = urllib.request.Request(
                api_url,
                data=body,
                headers={"Content-Type": "application/json"},
            )
            try:
                urllib.request.urlopen(req, timeout=10)
            except Exception as exc:
                log.error("Telegram send to %s failed: %s", chat_id, exc)
                ok = False
        if ok:
            delivered.append(house)
    return delivered


def send_telegram_alert(message: str) -> None:
    if not TELEGRAM_BOT_TOKEN:
        return
    raw = TELEGRAM_ALERT_CHAT_IDS or TELEGRAM_CHAT_IDS
    chat_ids = [c.strip() for c in raw.split(",") if c.strip()]
    if not chat_ids:
        return
    api_url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    for chat_id in chat_ids:
        body = json.dumps(
            {"chat_id": chat_id, "text": message, "parse_mode": "HTML"}
        ).encode()
        req = urllib.request.Request(
            api_url, data=body, headers={"Content-Type": "application/json"},
        )
        try:
            urllib.request.urlopen(req, timeout=10)
        except Exception as exc:
            log.error("Telegram alert to %s failed: %s", chat_id, exc)


def send_throttled_timeout_alert(key: str, message: str) -> None:
    """Send a per-site timeout alert at most once per TIMEOUT_ALERT_THROTTLE_SECONDS."""
    now = time.time()
    last = _last_timeout_alert.get(key, 0.0)
    if now - last < TIMEOUT_ALERT_THROTTLE_SECONDS:
        log.info(
            "Suppressing repeat timeout alert for %s (last sent %.0fs ago)",
            key, now - last,
        )
        return
    _last_timeout_alert[key] = now
    send_telegram_alert(message)


# ---------------------------------------------------------------------------
# Debug helper — dump raw HTML for selector development
# ---------------------------------------------------------------------------

def dump_html(page, name: str) -> None:
    if not DEBUG_DUMP:
        return
    dump_dir = Path("data/debug")
    dump_dir.mkdir(parents=True, exist_ok=True)
    path = dump_dir / f"{name}.html"
    path.write_bytes(page.body)
    log.info("Dumped HTML to %s (%d bytes)", path, len(page.body))


# ---------------------------------------------------------------------------
# Pararius parser
# ---------------------------------------------------------------------------

def scrape_pararius(page) -> list[dict[str, str]]:
    dump_html(page, "pararius")
    houses: list[dict[str, str]] = []

    listings = page.css("li.search-list__item--listing")
    if not listings:
        listings = page.css("section.listing-search-item")
    log.info("Pararius: %d listing elements found", len(listings))

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

            if city and not is_delft_area(city):
                continue

            price_el = listing.css(".listing-search-item__price-main")
            price = (price_el[0].text or "").strip() if price_el else ""

            area_el = listing.css(".illustrated-features__item--surface-area")
            area = (area_el[0].text or "").strip() if area_el else ""

            rooms_el = listing.css(
                ".illustrated-features__item--number-of-rooms"
            )
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
            log.warning("Pararius: failed to parse a listing: %s", exc)

    return houses


# ---------------------------------------------------------------------------
# Funda parser
# ---------------------------------------------------------------------------

def scrape_funda(page) -> list[dict[str, str]]:
    dump_html(page, "funda")
    houses: list[dict[str, str]] = []

    addr_links = page.css('[data-testid="listingDetailsAddress"]')
    log.info("Funda: %d listing elements found", len(addr_links))

    for addr_link in addr_links:
        try:
            href = addr_link.attrib.get("href", "")
            if not href:
                continue
            url = make_absolute(href, "https://www.funda.nl")

            addr_spans = addr_link.css("span.truncate")
            address = " ".join(
                (s.text or "").strip() for s in addr_spans
            ).strip()

            city_el = addr_link.css(".text-neutral-80")
            city = (city_el[0].text or "").strip() if city_el else "Delft"

            if city and not is_delft_area(city):
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
                    rooms = f"{txt} kamers" if int(txt) != 1 else "1 kamer"

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
            log.warning("Funda: failed to parse a listing: %s", exc)

    return houses


# ---------------------------------------------------------------------------
# VBT Verhuurmakelaars parser
# ---------------------------------------------------------------------------

def scrape_vbt(page) -> list[dict[str, str]]:
    dump_html(page, "vbt")
    houses: list[dict[str, str]] = []

    listings = page.css("a.property")
    log.info("VBT: %d listing elements found", len(listings))

    for listing in listings:
        try:
            href = listing.attrib.get("href", "")
            if not href:
                continue
            url = make_absolute(href, "https://vbtverhuurmakelaars.nl")

            items = listing.css(".items")
            if not items:
                continue
            container = items[0]

            city_el = container.css("div")
            city_raw = (
                (city_el[0].text or city_el[0].get_all_text() or "").strip()
                if city_el else ""
            )
            # css("div") can match a wrapper whose get_all_text() flattens the
            # whole listing; keep only the first line so the city stays a city.
            city = next(
                (line.strip() for line in city_raw.splitlines() if line.strip()),
                "",
            )

            addr_el = container.css("span.normal")
            address = (
                (addr_el[0].text or addr_el[0].get_all_text() or "").strip()
                if addr_el else ""
            )

            price_el = container.css(".price")
            price = (
                (price_el[0].text or price_el[0].get_all_text() or "").strip()
                if price_el else ""
            )

            area = ""
            rooms = ""
            rows = container.css("table tr")
            for row in rows:
                cells = row.css("td")
                if len(cells) >= 2:
                    label = (cells[0].text or "").strip().lower()
                    value = (cells[1].text or "").strip()
                    if "woonoppervlakte" in label:
                        area = value
                    elif "kamer" in label:
                        rooms = value

            if city and not is_delft_area(city):
                continue

            price_val = parse_price_euros(price)
            if price_val and price_val > MAX_PRICE:
                continue

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
            log.warning("VBT: failed to parse a listing: %s", exc)

    return houses


# ---------------------------------------------------------------------------
# Hof van Delft parser
# ---------------------------------------------------------------------------

def _scrape_realworks(page, base_url: str, site_name: str) -> list[dict[str, str]]:
    dump_html(page, site_name.lower().replace(" ", ""))
    houses: list[dict[str, str]] = []

    listings = _find_elements(page, "li.aanbodEntry", ".al2woning", ".al4woning")
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

            if "/koop/" in url or "/verkocht/" in url:
                continue

            address = _first_text(listing, "h3.street-address")
            city = _first_text(listing, "span.locality")

            if city and not is_delft_area(city):
                continue

            price_el = listing.css("span.kenmerkValue")
            price = (price_el[0].text or "").strip() if price_el else ""

            all_text = listing.get_all_text() or ""
            area = ""
            area_match = re.search(r"(\d+)\s*m[²2]", all_text)
            if area_match:
                area = f"{area_match.group(1)} m²"

            rooms = ""
            rooms_match = re.search(
                r"(\d+)\s*(?:slaap)?kamer", all_text, re.IGNORECASE
            )
            if rooms_match:
                rooms = rooms_match.group(0).strip()

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


def scrape_hofvandelft(page) -> list[dict[str, str]]:
    return _scrape_realworks(page, "https://www.hofvandelft.nl", "Hof van Delft")


# ---------------------------------------------------------------------------
# 123Wonen parser
# ---------------------------------------------------------------------------

def scrape_123wonen(page) -> list[dict[str, str]]:
    dump_html(page, "123wonen")
    houses: list[dict[str, str]] = []

    listings = page.css("div.pandlist-container")
    log.info("123Wonen: %d listing elements found", len(listings))

    for listing in listings:
        try:
            link_els = listing.css("a.textlink-design")
            if not link_els:
                link_els = listing.css("a")
            href = link_els[0].attrib.get("href", "") if link_els else ""
            if not href:
                onclick = listing.attrib.get("onclick", "")
                m = re.search(r"location\.href='([^']+)'", onclick)
                if m:
                    href = m.group(1)
            if not href:
                continue
            url = make_absolute(href, "https://www.123wonen.nl")

            address = _first_text(listing, "span.pand-address")

            title_el = listing.css("div.pand-title")
            city = ""
            if title_el:
                title_text = (title_el[0].text or "").strip()
                if "," in title_text:
                    city = title_text.split(",")[0].strip()

            if city and not is_delft_area(city):
                continue

            price = _first_text(listing, "div.pand-price")

            price_val = parse_price_euros(price)
            if price_val and price_val > MAX_PRICE:
                continue

            area = ""
            rooms = ""
            spec_items = listing.css("div.pand-specs li")
            for item in spec_items:
                spans = item.css("span")
                if len(spans) >= 2:
                    label = (spans[0].text or "").strip().lower()
                    value = (spans[1].text or "").strip()
                    if "oppervlakte" in label:
                        area = value
                    elif "slaapkamer" in label:
                        rooms = value

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
            log.warning("123Wonen: failed to parse a listing: %s", exc)

    return houses


# ---------------------------------------------------------------------------
# Rotsvast parser
# ---------------------------------------------------------------------------

def scrape_rotsvast(page) -> list[dict[str, str]]:
    dump_html(page, "rotsvast")
    houses: list[dict[str, str]] = []

    listings = page.css("a.card.card--house")
    log.info("Rotsvast: %d listing elements found", len(listings))

    for listing in listings:
        try:
            href = listing.attrib.get("href", "")
            if not href:
                continue
            url = make_absolute(href, "https://www.rotsvast.nl")

            address = _first_text(listing, ".card-house__title")

            text_els = listing.css(".card-house__text")
            city = (text_els[0].text or "").strip() if text_els else ""
            price = (text_els[-1].text or "").strip() if text_els else ""

            area = ""
            area_li = listing.css("li")
            for li in area_li:
                icons = li.css(".icon-surface")
                if icons:
                    area = (li.text or li.get_all_text() or "").strip()
                    break

            rooms = ""
            for li in area_li:
                icons = li.css(".icon-bed")
                if icons:
                    rooms = (li.text or li.get_all_text() or "").strip()
                    break

            if city and not is_delft_area(city):
                continue

            price_val = parse_price_euros(price)
            if price_val and price_val > MAX_PRICE:
                continue

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
            log.warning("Rotsvast: failed to parse a listing: %s", exc)

    return houses


# ---------------------------------------------------------------------------
# Prinsenstad Makelaardij parser (Haystack platform)
# ---------------------------------------------------------------------------

def scrape_prinsenstad(page) -> list[dict[str, str]]:
    dump_html(page, "prinsenstad")
    houses: list[dict[str, str]] = []

    listings = _find_elements(
        page,
        ".object_list_container .object_data",
        ".object_list_container a[href*='/object/']",
        "div.object_list a[href]",
    )
    log.info("Prinsenstad: %d listing elements found", len(listings))

    for listing in listings:
        try:
            href = listing.attrib.get("href", "")
            if not href:
                link_els = listing.css("a[href]")
                if link_els:
                    href = link_els[0].attrib.get("href", "")
            if not href:
                continue
            url = make_absolute(href, "https://prinsenstadmakelaardij.nl")

            all_text = listing.get_all_text() or ""
            address = _first_text(
                listing, ".street_name", ".address", "h2", "h3"
            )
            city = _first_text(listing, ".city", ".locality")

            if city and not is_delft_area(city):
                continue

            price = ""
            price_match = re.search(r"€\s*[\d.,]+", all_text)
            if price_match:
                price = price_match.group(0).strip()

            area = ""
            area_match = re.search(r"(\d+)\s*m[²2]", all_text)
            if area_match:
                area = f"{area_match.group(1)} m²"

            rooms = ""
            rooms_match = re.search(
                r"(\d+)\s*(?:slaap)?kamer", all_text, re.IGNORECASE
            )
            if rooms_match:
                rooms = rooms_match.group(0).strip()

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
            log.warning("Prinsenstad: failed to parse a listing: %s", exc)

    return houses


# ---------------------------------------------------------------------------
# Pactum Vastgoed parser (Webflow)
# ---------------------------------------------------------------------------

def scrape_pactum(page) -> list[dict[str, str]]:
    dump_html(page, "pactum")
    houses: list[dict[str, str]] = []

    listings = _find_elements(
        page,
        ".w-dyn-item",
        ".collection-item",
    )
    log.info("Pactum: %d listing elements found", len(listings))

    for listing in listings:
        try:
            href = ""
            link_els = listing.css("a")
            if link_els:
                href = link_els[0].attrib.get("href", "")
            if not href:
                href = listing.attrib.get("href", "")
            if not href:
                continue
            url = make_absolute(href, "https://www.pactumvastgoed.nl")

            all_text = listing.get_all_text() or ""
            address = _first_text(
                listing, "h2", "h3", ".address", ".title", ".heading"
            )

            city = ""
            if is_delft_area(
                _first_text(listing, ".city", ".location", ".plaats")
            ):
                city = _first_text(listing, ".city", ".location", ".plaats")
            elif re.search(
                r"Delft|Delfgauw|Den Hoorn|Rijswijk|Schipluiden|Nootdorp|Pijnacker",
                all_text,
                re.IGNORECASE,
            ):
                match = re.search(
                    r"(Delft|Delfgauw|Den Hoorn|Rijswijk|Schipluiden|Nootdorp|Pijnacker)",
                    all_text,
                    re.IGNORECASE,
                )
                city = match.group(1).title() if match else ""

            if city and not is_delft_area(city):
                continue

            price = ""
            price_match = re.search(r"€\s*[\d.,]+", all_text)
            if price_match:
                price = price_match.group(0).strip()

            price_val = parse_price_euros(price)
            if price_val and price_val > MAX_PRICE:
                continue

            area = ""
            area_match = re.search(r"(\d+)\s*m[²2]", all_text)
            if area_match:
                area = f"{area_match.group(1)} m²"

            rooms = ""
            rooms_match = re.search(
                r"(\d+)\s*(?:slaap)?kamer", all_text, re.IGNORECASE
            )
            if rooms_match:
                rooms = rooms_match.group(0).strip()

            houses.append(
                {
                    "url": url,
                    "straatnaamHuisnummer": address or "Onbekend",
                    "plaats": city or "Onbekend",
                    "vraagprijs": price,
                    "oppervlakte": area,
                    "kamers": rooms,
                }
            )
        except Exception as exc:
            log.warning("Pactum: failed to parse a listing: %s", exc)

    return houses


# ---------------------------------------------------------------------------
# VW Makelaars Delft parser
# ---------------------------------------------------------------------------

def scrape_vwmakelaars(page) -> list[dict[str, str]]:
    return _scrape_realworks(page, "https://delft.vwmakelaars.nl", "VW Makelaars")


# ---------------------------------------------------------------------------
# Rent a Room Delft parser (WordPress / Estatik)
# ---------------------------------------------------------------------------

def scrape_rentaroom(page) -> list[dict[str, str]]:
    dump_html(page, "rentaroom")
    houses: list[dict[str, str]] = []

    listings = page.css("div.item-listing-wrap")
    log.info("Rent a Room: %d listing elements found", len(listings))

    for listing in listings:
        try:
            title_link = listing.css("h2.item-title a")
            if not title_link:
                continue
            href = title_link[0].attrib.get("href", "")
            if not href:
                continue
            url = make_absolute(href, "https://rent-a-room-delft.nl")

            title_text = (title_link[0].text or "").strip()
            address = title_text
            city = ""
            if "," in title_text:
                address = title_text.rsplit(",", 1)[0].strip()
                city = title_text.rsplit(",", 1)[1].strip()

            if city and not is_delft_area(city):
                continue

            price = _first_text(listing, "li.item-price")

            area_el = listing.css("li.h-area span.hz-figure")
            area = f"{(area_el[0].text or '').strip()} m²" if area_el else ""

            rooms = ""

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
            log.warning("Rent a Room: failed to parse a listing: %s", exc)

    return houses


# ---------------------------------------------------------------------------
# Frisia Makelaars discovery via sitemap + per-listing plain HTTP
# ---------------------------------------------------------------------------
# Frisia's listings index (/wonen/aanbod?...) is refused at the TCP layer for
# datacenter IPs running a stealth browser. The XML sitemap and individual
# property detail pages are still served over plain HTTP, so we walk those.

_SITEMAP_NS = {"sm": "http://www.sitemaps.org/schemas/sitemap/0.9"}
_FRISIA_SITEMAP_NS = _SITEMAP_NS


def _parse_frisia_listing(url: str, body: bytes) -> dict[str, str] | None:
    """Extract one Frisia rental from a detail-page HTML body, or None to skip."""
    a = Adaptor(content=body, url=url)

    # Rent vs sale: rentals carry a panel-block feature labelled "Huurprijs".
    rent_block = next(
        (
            b for b in a.css(".panel__block__feature")
            if "Huurprijs" in b.get_all_text(separator=" ", strip=True)
        ),
        None,
    )
    if rent_block is None:
        return None

    price_line = rent_block.get_all_text(separator=" ", strip=True)
    price = price_line.split("Huurprijs", 1)[-1].strip(" |")
    price_val = parse_price_euros(price)
    if price_val and price_val > MAX_PRICE:
        return None

    h1 = a.css("h1")
    full_address = h1[0].get_all_text(separator=" ", strip=True) if h1 else ""
    # Typical h1 text: "Plesmanweg 611 , 2597 JG, 's-Gravenhage"
    parts = [p.strip() for p in full_address.split(",") if p.strip()]
    street = parts[0] if parts else "Onbekend"
    city = parts[-1] if len(parts) >= 2 else ""
    if city and not is_delft_area(city):
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


def scrape_frisia_via_sitemap(existing_urls: set[str]) -> list[dict[str, str]]:
    try:
        sitemap = _http_get(FRISIA_SITEMAP_URL)
    except Exception as exc:
        log.warning("Frisia: sitemap fetch failed: %s", exc)
        return []

    try:
        root = ET.fromstring(sitemap)
    except ET.ParseError as exc:
        log.warning("Frisia: sitemap parse failed: %s", exc)
        return []

    all_urls: list[str] = []
    for u in root.findall("sm:url", _FRISIA_SITEMAP_NS):
        loc = u.find("sm:loc", _FRISIA_SITEMAP_NS)
        if loc is not None and loc.text:
            all_urls.append(loc.text)
    log.info("Frisia: sitemap has %d total URLs", len(all_urls))

    # The slug embeds the city, so we can drop most listings (Den Haag etc.)
    # without spending a request on them. False positives just cost one extra
    # fetch and are discarded after parsing the real city out of the page.
    candidates = [u for u in all_urls if is_delft_area(u.replace("-", " "))]
    new_candidates = [u for u in candidates if u not in existing_urls]
    log.info(
        "Frisia: %d candidates in Delft area, %d new",
        len(candidates), len(new_candidates),
    )

    if len(new_candidates) > FRISIA_MAX_FETCHES_PER_CYCLE:
        log.info(
            "Frisia: capping detail fetches at %d this cycle",
            FRISIA_MAX_FETCHES_PER_CYCLE,
        )
        new_candidates = new_candidates[:FRISIA_MAX_FETCHES_PER_CYCLE]

    houses: list[dict[str, str]] = []
    for url in new_candidates:
        try:
            body = _http_get(url)
        except Exception as exc:
            log.warning("Frisia: detail fetch failed for %s: %s", url, exc)
            continue
        try:
            listing = _parse_frisia_listing(url, body)
        except Exception as exc:
            log.warning("Frisia: parse failed for %s: %s", url, exc)
            continue
        if listing is not None:
            houses.append(listing)

    log.info("Frisia: %d new rental match(es)", len(houses))
    return houses


# ---------------------------------------------------------------------------
# Marloes Makelaars via sitemap + per-listing plain HTTP
# ---------------------------------------------------------------------------
# The /aanbod/huur/ index renders fine via Camoufox, but the WordPress
# property CPT sitemap covers the same listings without launching a browser.
# Marloes mixes sales and rentals in one feed — rentals say "per maand" in
# the Prijs row, sales say "kosten koper"/"k.k."/"v.o.n.".

def _parse_marloes_listing(url: str, body: bytes) -> dict[str, str] | None:
    """Extract one Marloes rental from a detail-page HTML body, or None to skip."""
    a = Adaptor(content=body, url=url)

    fields: dict[str, str] = {}
    for dt, dd in zip(a.css("dl dt"), a.css("dl dd")):
        label = (dt.text or "").strip().lower()
        if not label:
            continue
        val = (dd.text or "").strip() or (dd.get_all_text() or "").strip()
        fields[label] = val

    price = fields.get("prijs", "")
    if "per maand" not in price.lower():
        return None

    price_val = parse_price_euros(price)
    if price_val and price_val > MAX_PRICE:
        return None

    city = fields.get("plaats", "")
    if city and not is_delft_area(city):
        return None

    # <title> is "Street Number te CITY | Marloes Makelaars" — the H1 only
    # carries the street name, so the title is the most reliable address source.
    title_els = a.css("title")
    title = (title_els[0].text or "").strip() if title_els else ""
    address = title.split(" | ", 1)[0].strip()
    if city:
        address = re.sub(
            rf"\s+te\s+{re.escape(city)}\s*$", "", address, flags=re.I
        ).strip()

    rooms = fields.get("slaapkamers", "")
    if rooms.isdigit():
        rooms = f"{rooms} slaapkamer" if rooms == "1" else f"{rooms} slaapkamers"

    return {
        "url": url,
        "straatnaamHuisnummer": address or "Onbekend",
        "plaats": (city or "Delft").title(),
        "vraagprijs": price,
        "oppervlakte": fields.get("oppervlakte", ""),
        "kamers": rooms,
    }


def scrape_marloes_via_sitemap(existing_urls: set[str]) -> list[dict[str, str]]:
    try:
        sitemap = _http_get(MARLOES_SITEMAP_URL)
    except Exception as exc:
        log.warning("Marloes: sitemap fetch failed: %s", exc)
        return []

    try:
        root = ET.fromstring(sitemap)
    except ET.ParseError as exc:
        log.warning("Marloes: sitemap parse failed: %s", exc)
        return []

    all_urls: list[str] = []
    for u in root.findall("sm:url", _SITEMAP_NS):
        loc = u.find("sm:loc", _SITEMAP_NS)
        if loc is not None and loc.text:
            all_urls.append(loc.text)
    log.info("Marloes: sitemap has %d total URLs", len(all_urls))

    # Slugs carry the city ("…-te-delft/"), so non-Delft listings can be
    # dropped without spending a fetch on them.
    candidates = [u for u in all_urls if is_delft_area(u.replace("-", " "))]
    new_candidates = [u for u in candidates if u not in existing_urls]
    log.info(
        "Marloes: %d candidates in Delft area, %d new",
        len(candidates), len(new_candidates),
    )

    houses: list[dict[str, str]] = []
    for url in new_candidates:
        try:
            body = _http_get(url)
        except Exception as exc:
            log.warning("Marloes: detail fetch failed for %s: %s", url, exc)
            continue
        try:
            listing = _parse_marloes_listing(url, body)
        except Exception as exc:
            log.warning("Marloes: parse failed for %s: %s", url, exc)
            continue
        if listing is not None:
            houses.append(listing)

    log.info("Marloes: %d new rental match(es)", len(houses))
    return houses


# ---------------------------------------------------------------------------
# Oude Delft parser (WordPress + AngularJS LSCF plugin)
# Content is rendered client-side by AngularJS; StealthyFetcher may not
# trigger the Angular digest cycle, so this parser may return 0 results.
# ---------------------------------------------------------------------------

def scrape_oudedelft(page) -> list[dict[str, str]]:
    dump_html(page, "oudedelft")
    houses: list[dict[str, str]] = []

    listings = _find_elements(
        page,
        ".lscf-posts-wrapper a[href*='oudedelft.com']",
        ".lscf-custom-template-wrapper a[href]",
    )
    log.info("Oude Delft: %d listing elements found", len(listings))

    for listing in listings:
        try:
            href = listing.attrib.get("href", "")
            if not href:
                link_els = listing.css("a")
                if link_els:
                    href = link_els[0].attrib.get("href", "")
            if not href:
                continue
            url = make_absolute(href, "https://oudedelft.com")

            all_text = listing.get_all_text() or ""
            address = _first_text(
                listing, "h2", "h3", ".address", ".title", ".heading"
            )

            city = ""
            city_match = re.search(
                r"(Delft|Delfgauw|Den Hoorn|Rijswijk|Schipluiden|Nootdorp|Pijnacker)",
                all_text,
                re.IGNORECASE,
            )
            if city_match:
                city = city_match.group(1).title()

            price = ""
            price_match = re.search(r"€\s*[\d.,]+", all_text)
            if price_match:
                price = price_match.group(0).strip()

            price_val = parse_price_euros(price)
            if price_val and price_val > MAX_PRICE:
                continue

            area = ""
            area_match = re.search(r"(\d+)\s*m[²2]", all_text)
            if area_match:
                area = f"{area_match.group(1)} m²"

            rooms = ""
            rooms_match = re.search(
                r"(\d+)\s*(?:slaap)?kamer", all_text, re.IGNORECASE
            )
            if rooms_match:
                rooms = rooms_match.group(0).strip()

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
            log.warning("Oude Delft: failed to parse a listing: %s", exc)

    return houses


# ---------------------------------------------------------------------------
# PSG Wonen parser (Haystack platform)
# ---------------------------------------------------------------------------

def scrape_psgwonen(page) -> list[dict[str, str]]:
    dump_html(page, "psgwonen")
    houses: list[dict[str, str]] = []

    listings = page.css("article")
    log.info("PSG Wonen: %d listing elements found", len(listings))

    for listing in listings:
        try:
            link_els = listing.css("div.datacontainer a")
            if not link_els:
                continue
            href = link_els[0].attrib.get("href", "")
            if not href:
                continue
            url = make_absolute(href, "https://www.psg-wonen.nl")

            raw_address = _first_text(listing, "h3.obj_address")
            address = re.sub(
                r"^(Onder bod|Te huur|Verhuurd|Nieuw in verhuur)\s*:\s*",
                "",
                raw_address,
                flags=re.IGNORECASE,
            ).strip()

            city = ""
            city_match = re.search(r"\d{4}\s*[A-Z]{2}\s+(.+)$", address)
            if city_match:
                city = city_match.group(1).strip()
                address = address[: city_match.start()].strip().rstrip(",")

            price = _first_text(listing, "span.obj_price")

            rooms_el = listing.css("span.object_rooms span")
            rooms = (rooms_el[-1].text or "").strip() if rooms_el else ""
            if rooms and rooms.isdigit():
                rooms = f"{rooms} kamers" if int(rooms) != 1 else "1 kamer"

            area_el = listing.css("span.object_sqfeet span[title]")
            area = (area_el[0].text or "").strip() if area_el else ""

            if city and not is_delft_area(city):
                continue

            price_val = parse_price_euros(price)
            if price_val and price_val > MAX_PRICE:
                continue

            houses.append(
                {
                    "url": url,
                    "straatnaamHuisnummer": address or "Onbekend",
                    "plaats": city or "Onbekend",
                    "vraagprijs": price,
                    "oppervlakte": area,
                    "kamers": rooms,
                }
            )
        except Exception as exc:
            log.warning("PSG Wonen: failed to parse a listing: %s", exc)

    return houses


# ---------------------------------------------------------------------------
# Van Gulden Makelaardij parser (MTMO / WordPress)
# ---------------------------------------------------------------------------

def scrape_vangulden(page) -> list[dict[str, str]]:
    dump_html(page, "vangulden")
    houses: list[dict[str, str]] = []

    listings = page.css('a[href*="aanbod-detail"]')
    log.info("Van Gulden: %d listing elements found", len(listings))

    for listing in listings:
        try:
            href = listing.attrib.get("href", "")
            if not href:
                continue
            url = make_absolute(href, "https://vanguldenmakelaardij.nl")

            address = _first_text(listing, "div.titel")
            city = _first_text(listing, "p.notranslate")

            price = _first_text(listing, "div.price")

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

            if city and not is_delft_area(city):
                continue

            price_val = parse_price_euros(price)
            if price_val and price_val > MAX_PRICE:
                continue

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
            log.warning("Van Gulden: failed to parse a listing: %s", exc)

    return houses


# ---------------------------------------------------------------------------
# ZO Makelaars parser (Realworks platform)
# ---------------------------------------------------------------------------

def scrape_zomakelaars(page) -> list[dict[str, str]]:
    return _scrape_realworks(
        page, "https://www.zomakelaars.nl", "ZO Makelaars"
    )


# ---------------------------------------------------------------------------
# ikwilhuren.nu parser (MVGM platform)
# ---------------------------------------------------------------------------

def scrape_ikwilhuren(page) -> list[dict[str, str]]:
    dump_html(page, "ikwilhuren")
    houses: list[dict[str, str]] = []

    listings = page.css(".card.card-woning")
    log.info("ikwilhuren.nu: %d listing elements found", len(listings))

    for listing in listings:
        try:
            link_els = listing.css("a.stretched-link")
            if not link_els:
                continue
            href = link_els[0].attrib.get("href", "")
            if not href:
                continue
            url = make_absolute(href, "https://ikwilhuren.nu")

            title = (link_els[0].text or link_els[0].get_all_text() or "").strip()
            address = re.sub(
                r"^(Appartement|Eengezinswoning|Woonhuis|Studio)\s+",
                "",
                title,
                flags=re.IGNORECASE,
            ).strip()

            city_el = listing.css(".card-body > span:nth-child(2)")
            raw_city = (
                (city_el[0].text or city_el[0].get_all_text() or "").strip()
                if city_el else ""
            )
            city = re.sub(r"^\d{4}\s*[A-Z]{2}\s+", "", raw_city)
            city = re.sub(r"\s*-\s*\d+\s*Km\.?$", "", city).strip()

            price = _first_text(listing, ".dotted-spans .fw-bold")

            all_text = listing.get_all_text() or ""
            area = ""
            area_match = re.search(r"(\d+)\s*m[²2\s]", all_text)
            if area_match:
                area = f"{area_match.group(1)} m²"

            rooms = ""
            rooms_match = re.search(
                r"(\d+)\s*slaapkamer", all_text, re.IGNORECASE
            )
            if rooms_match:
                rooms = rooms_match.group(0).strip()

            if city and not is_delft_area(city):
                continue

            price_val = parse_price_euros(price)
            if price_val and price_val > MAX_PRICE:
                continue

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
            log.warning("ikwilhuren.nu: failed to parse a listing: %s", exc)

    return houses


# ---------------------------------------------------------------------------
# 070 Wonen parser (WordPress)
# ---------------------------------------------------------------------------

def scrape_070wonen(page) -> list[dict[str, str]]:
    dump_html(page, "070wonen")
    houses: list[dict[str, str]] = []

    listings = page.css("li.New, li.Onder\\ optie")
    if not listings:
        listings = [
            li for li in page.css("li")
            if li.css("h3") and li.css("a[href*='/huurwoningen/']")
        ]
    log.info("070 Wonen: %d listing elements found", len(listings))

    for listing in listings:
        try:
            cls = (listing.attrib.get("class", "") or "").lower()
            if "verhuurd" in cls:
                continue

            link_els = listing.css("a[href*='/huurwoningen/']")
            if not link_els:
                continue
            href = link_els[0].attrib.get("href", "")
            if not href:
                continue
            url = make_absolute(href, "https://www.070wonen.nl")

            heading = _first_text(listing, "h3")
            address = heading
            city = ""
            pc_match = re.search(r"^(.*?)\s+(\d{4}\s*[A-Z]{2})\s+(.+)$", heading)
            if pc_match:
                address = pc_match.group(1).strip()
                city = pc_match.group(3).strip()

            if city and not is_delft_area(city):
                continue

            all_text = listing.get_all_text() or ""
            price = ""
            price_match = re.search(r"€\s*[\d.,]+", all_text)
            if price_match:
                price = price_match.group(0).strip()

            price_val = parse_price_euros(price)
            if price_val and price_val > MAX_PRICE:
                continue

            area = ""
            rooms = ""
            detail_items = listing.css("ul li")
            for item in detail_items:
                txt = (item.text or item.get_all_text() or "").strip()
                if re.search(r"\d+\s*m[²2]", txt):
                    area = txt
                elif re.search(r"slaapkamer", txt, re.IGNORECASE):
                    rooms = txt

            houses.append(
                {
                    "url": url,
                    "straatnaamHuisnummer": address or "Onbekend",
                    "plaats": city or "Onbekend",
                    "vraagprijs": price,
                    "oppervlakte": area,
                    "kamers": rooms,
                }
            )
        except Exception as exc:
            log.warning("070 Wonen: failed to parse a listing: %s", exc)

    return houses


# ---------------------------------------------------------------------------
# De Bruyn en Tak Makelaardij parser
# ---------------------------------------------------------------------------

def scrape_debruynentak(page) -> list[dict[str, str]]:
    dump_html(page, "debruynentak")
    houses: list[dict[str, str]] = []

    # Listing cards: div.objectList > div.item. Each card holds a status
    # label, an a.itemTitel with span.objectTitel (address) + span.itemSubtitel
    # (city), a span.itemSpecs ("3 kamer appartement, 86 m²") and a
    # div.itemPrice with span.price ("1.325,-").
    items = page.css("div.objectList div.item")
    log.info("De Bruyn en Tak: %d listing elements found", len(items))

    for item in items:
        try:
            # A "Verhuurd" / "Verhuurd onder voorbehoud" label means rented.
            label = _first_text(item, "div.label")
            if re.search(r"verhuurd", label, re.IGNORECASE):
                continue

            link_el = item.css("a.itemTitel")
            if not link_el:
                continue
            href = link_el[0].attrib.get("href", "")
            if not href:
                continue
            url = make_absolute(href, "https://www.debruynentak.nl")

            address = _first_text(item, "span.objectTitel")
            if not address:
                slug = href.rsplit("/", 1)[-1].removesuffix(".html")
                address = slug.replace("-", " ").strip().title()

            city = _first_text(item, "span.itemSubtitel") or "Delft"

            if city and not is_delft_area(city):
                continue

            price_txt = _first_text(item, "div.itemPrice .price")
            price = f"€ {price_txt}" if price_txt else ""
            price_val = parse_price_euros(price)
            if price_val and price_val > MAX_PRICE:
                continue

            specs = _first_text(item, "span.itemSpecs")

            area = ""
            area_match = re.search(r"(\d+)\s*m[²2]", specs)
            if area_match:
                area = f"{area_match.group(1)} m²"

            rooms = ""
            rooms_match = re.search(
                r"(\d+)\s*(?:slaap)?kamer", specs, re.IGNORECASE
            )
            if rooms_match:
                rooms = rooms_match.group(0).strip()

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
            log.warning("De Bruyn en Tak: failed to parse a listing: %s", exc)

    return houses


# ---------------------------------------------------------------------------
# Nationaal Grondbezit parser
# Detail URLs look like /huuraanbod/<City>/<address-slug>. The site's own
# location filter only works through the front page, so the whole overview is
# fetched and narrowed with is_delft_area/MAX_PRICE.
# ---------------------------------------------------------------------------

def scrape_nationaalgrondbezit(page) -> list[dict[str, str]]:
    from urllib.parse import unquote

    dump_html(page, "nationaalgrondbezit")
    houses: list[dict[str, str]] = []

    detail_re = re.compile(r"/huuraanbod/[^/]+/[^/]+$")

    def _listing_hrefs(el) -> set[str]:
        found: set[str] = set()
        for a in el.css('a[href*="/huuraanbod/"]'):
            href = a.attrib.get("href", "")
            if detail_re.search(href.split("?")[0].rstrip("/")):
                found.add(href)
        return found

    anchors_by_href: dict[str, object] = {}
    for a in page.css('a[href*="/huuraanbod/"]'):
        href = a.attrib.get("href", "")
        if href and href not in anchors_by_href and detail_re.search(
            href.split("?")[0].rstrip("/")
        ):
            anchors_by_href[href] = a
    log.info(
        "Nationaal Grondbezit: %d listing elements found", len(anchors_by_href)
    )

    for href, anchor in anchors_by_href.items():
        try:
            url = make_absolute(href, "https://www.nationaalgrondbezit.nl")

            path = href.split("?")[0].rstrip("/")
            m = detail_re.search(path)
            city_slug, addr_slug = m.group(0).split("/")[2:4]
            city = unquote(city_slug).replace("-", " ").strip().title()

            if city and not is_delft_area(city):
                continue

            # Widen to the largest ancestor that still holds only this
            # listing's links, so price/area text outside the anchor is in
            # scope without pulling in a neighbouring card.
            card = anchor
            for _ in range(6):
                parent = card.parent
                if parent and _listing_hrefs(parent) == {href}:
                    card = parent
                else:
                    break

            address = _first_text(card, "h2", "h3", ".title", ".address")
            if not address:
                address = unquote(addr_slug).replace("-", " ").strip().title()

            all_text = card.get_all_text() or ""

            price = ""
            price_match = re.search(r"€\s*[\d.,]+", all_text)
            if price_match:
                price = price_match.group(0).strip()

            price_val = parse_price_euros(price)
            if price_val and price_val > MAX_PRICE:
                continue

            area = ""
            area_match = re.search(r"(\d+)\s*m[²2]", all_text)
            if area_match:
                area = f"{area_match.group(1)} m²"

            rooms = ""
            rooms_match = re.search(
                r"(\d+)\s*(?:slaap)?kamer", all_text, re.IGNORECASE
            )
            if rooms_match:
                rooms = rooms_match.group(0).strip()

            houses.append(
                {
                    "url": url,
                    "straatnaamHuisnummer": address or "Onbekend",
                    "plaats": city or "Onbekend",
                    "vraagprijs": price,
                    "oppervlakte": area,
                    "kamers": rooms,
                }
            )
        except Exception as exc:
            log.warning(
                "Nationaal Grondbezit: failed to parse a listing: %s", exc
            )

    return houses


# ---------------------------------------------------------------------------
# Main loop
# ---------------------------------------------------------------------------

SITES = [
    ("Pararius", PARARIUS_URL, scrape_pararius),
    ("Funda", FUNDA_URL, scrape_funda),
    ("VBT Verhuurmakelaars", VBT_URL, scrape_vbt),
    ("Hof van Delft", HOFVANDELFT_URL, scrape_hofvandelft),
    ("123Wonen", EENTWEEDRIEWONEN_URL, scrape_123wonen),
    ("Rotsvast", ROTSVAST_URL, scrape_rotsvast),
    ("Prinsenstad Makelaardij", PRINSENSTAD_URL, scrape_prinsenstad),
    ("Pactum Vastgoed", PACTUM_URL, scrape_pactum),
    ("VW Makelaars", VWMAKELAARS_URL, scrape_vwmakelaars),
    ("Rent a Room Delft", RENTAROOM_URL, scrape_rentaroom),
    ("Oude Delft", OUDEDELFT_URL, scrape_oudedelft),
    ("PSG Wonen", PSGWONEN_URL, scrape_psgwonen),
    ("Van Gulden Makelaardij", VANGULDEN_URL, scrape_vangulden),
    ("ZO Makelaars", ZOMAKELAARS_URL, scrape_zomakelaars),
    ("ikwilhuren.nu", IKWILHUREN_URL, scrape_ikwilhuren),
    ("070 Wonen", ZEVENTIGWONEN_URL, scrape_070wonen),
    ("De Bruyn en Tak", DEBRUYNENTAK_URL, scrape_debruynentak),
    ("Nationaal Grondbezit", NATIONAALGRONDBEZIT_URL, scrape_nationaalgrondbezit),
]

# Sites that don't fit the StealthyFetcher → parser pattern. Each entry is a
# function (existing_urls) -> list[house] that handles its own fetching.
CUSTOM_SITES = [
    ("Frisia Makelaars", scrape_frisia_via_sitemap),
    ("Marloes Makelaars", scrape_marloes_via_sitemap),
]


def _fetch_with_timeout(url: str) -> object:
    global _consecutive_fetch_failures
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
        # Playwright/Camoufox sometimes wedges the browser; once that happens
        # every subsequent fetch hangs. Count these alongside timeouts so the
        # self-heal threshold can trip from either symptom.
        if "Page crashed" in str(exc):
            _record_fetch_failure(url, "page crashed")
        raise
    _consecutive_fetch_failures = 0
    _failed_urls_in_streak.clear()
    return page


def _record_fetch_failure(url: str, reason: str) -> None:
    global _consecutive_fetch_failures
    # The wedge detector trips on the *browser* being broken, not on a single
    # site being down. If the same URL fails again before any success resets
    # the streak, treat it as the site's problem and don't count it.
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
            send_telegram_alert(f"♻️ <b>Sidecar self-restart</b> — {msg}")
        except Exception:
            log.exception("Failed to send self-restart Telegram alert")
        try:
            SELF_RESTART_MARKER.parent.mkdir(parents=True, exist_ok=True)
            SELF_RESTART_MARKER.write_text(reason)
        except Exception:
            log.exception("Failed to write self-restart marker")
        # Hard exit: the wedged fetch still occupies the _fetch_pool worker
        # thread. sys.exit() would hang forever in the concurrent.futures
        # atexit handler joining that thread, so the process never dies and
        # Docker never restarts it. os._exit() skips atexit/thread-join.
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
        except TimeoutError:
            log.warning("%s page %d timed out, retrying once ...", name, page_num)
            try:
                page = _fetch_with_timeout(url)
            except TimeoutError:
                log.warning("%s page %d timed out after %ds, skipping", name, page_num, FETCH_TIMEOUT)
                send_throttled_timeout_alert(
                    f"{name}#page{page_num}",
                    f"⚠️ <b>{name}</b> page {page_num} timed out after {FETCH_TIMEOUT}s — skipped",
                )
                break
        page_houses = parser(page)
        if not page_houses:
            break

        new_on_page = [
            h for h in page_houses
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


def _deliver_new_houses(conn, name, houses, existing_urls, all_new):
    """Send new houses to Telegram, persist what was delivered, and log misses."""
    if not houses:
        return
    sent = send_telegram(houses)
    if sent:
        save_houses(conn, sent)
        all_new.extend(sent)
        existing_urls.update(h["url"] for h in sent)
    unsent = len(houses) - len(sent)
    if unsent:
        log.warning(
            "%s: %d listing(s) not saved — Telegram send failed, "
            "will retry next cycle", name, unsent,
        )


def run_cycle():
    conn = init_db()
    existing_urls = get_existing_urls(conn)
    all_new = []

    for name, url, parser in SITES:
        try:
            if "{page}" in url:
                houses = _scrape_paginated(name, url, parser, existing_urls)
                log.info("%s: %d new across all pages", name, len(houses))
            else:
                log.info("Fetching %s ...", name)
                try:
                    page = _fetch_with_timeout(url)
                except TimeoutError:
                    log.warning("%s timed out, retrying once ...", name)
                    page = _fetch_with_timeout(url)
                all_houses = parser(page)
                houses = [h for h in all_houses if h["url"] not in existing_urls]
                log.info("%s: %d scraped, %d new", name, len(all_houses), len(houses))

            _deliver_new_houses(conn, name, houses, existing_urls, all_new)
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
            _deliver_new_houses(conn, name, houses, existing_urls, all_new)
        except Exception as exc:
            log.error("%s scrape failed: %s", name, exc, exc_info=True)

    conn.close()
    return all_new


def main():
    log.info(
        "Starting scraper sidecar (%d sites)  db=%s  interval=%ds  debug=%s",
        len(SITES),
        DB_PATH,
        CHECK_INTERVAL,
        DEBUG_DUMP,
    )

    if SELF_RESTART_MARKER.exists():
        try:
            reason = SELF_RESTART_MARKER.read_text().strip() or "unknown"
            send_telegram_alert(
                f"✅ <b>Sidecar back online</b> — recovered after self-restart "
                f"(trigger: {reason})"
            )
        except Exception:
            log.exception("Failed to send self-restart recovery alert")
        finally:
            SELF_RESTART_MARKER.unlink(missing_ok=True)

    while True:
        try:
            new = run_cycle()
            log.info("Cycle done — %d new listing(s)", len(new))
        except Exception as exc:
            log.error("Cycle failed: %s", exc, exc_info=True)
        time.sleep(CHECK_INTERVAL)


if __name__ == "__main__":
    main()
