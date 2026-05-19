import json
import logging
import os
import re
import sqlite3
import sys
import time
import urllib.request
from concurrent.futures import ThreadPoolExecutor, TimeoutError
from pathlib import Path

from scrapling.fetchers import StealthyFetcher

FETCH_TIMEOUT = int(os.environ.get("FETCH_TIMEOUT", "120"))
MAX_CONSECUTIVE_FETCH_FAILURES = int(
    os.environ.get("MAX_CONSECUTIVE_FETCH_FAILURES", "3")
)
_fetch_pool = ThreadPoolExecutor(max_workers=1)
_consecutive_fetch_failures = 0

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
)
log = logging.getLogger("sidecar")

DB_PATH = os.environ.get("DB_PATH", "data/db.sqlite")
CHECK_INTERVAL = int(os.environ.get("CHECK_INTERVAL", "300"))
DEBUG_DUMP = os.environ.get("DEBUG_DUMP", "").lower() in ("1", "true", "yes")
TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_IDS = os.environ.get("TELEGRAM_CHAT_IDS", "")

# ---------------------------------------------------------------------------
# Site URLs
# ---------------------------------------------------------------------------

PARARIUS_URL = "https://www.pararius.nl/huurwoningen/delft/0-1500"
FUNDA_URL = (
    "https://www.funda.nl/zoeken/huur"
    "?selected_area=%5B%22delft%22%5D&price=%220-1500%22"
)
VBT_URL = "https://vbtverhuurmakelaars.nl/woningen?city=delft&maxPrice=1500"
MARLOES_URL = (
    "https://www.marloesmakelaars.nl/aanbod/huur/"
    "?interior=&bedrooms=&min_price=&max_price=1500&city=DELFT&address="
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
FRISIA_URL = (
    "https://frisiamakelaars.nl/wonen/aanbod/"
    "?buy_rent=rent&rent_price=-1500&distance=5"
    "&search=delft&order_by=created_at-desc&page={page}"
)
OUDEDELFT_URL = "https://oudedelft.com/huur-2/"
PSGWONEN_URL = "https://www.psg-wonen.nl/woningaanbod/huur"
VANGULDEN_URL = "https://vanguldenmakelaardij.nl/huuraanbod/"
ZOMAKELAARS_URL = (
    "https://www.zomakelaars.nl/aanbod/woningaanbod/vestiging-906351/huur/"
)
IKWILHUREN_URL = "https://ikwilhuren.nu/aanbod/delft/"
ZEVENTIGWONEN_URL = "https://www.070wonen.nl/huurwoningen/"

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


def is_delft_area(city: str) -> bool:
    return city.strip().lower() in DELFT_AREA_CITIES


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
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_IDS:
        log.warning("Telegram not configured, skipping notifications")
        return

    chat_ids = [c.strip() for c in TELEGRAM_CHAT_IDS.split(",") if c.strip()]
    api_url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"

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


def send_telegram_alert(message: str) -> None:
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_IDS:
        return
    chat_ids = [c.strip() for c in TELEGRAM_CHAT_IDS.split(",") if c.strip()]
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
            city = (
                (city_el[0].text or city_el[0].get_all_text() or "").strip()
                if city_el else ""
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
# Marloes Makelaars parser
# ---------------------------------------------------------------------------

def scrape_marloes(page) -> list[dict[str, str]]:
    dump_html(page, "marloes")
    houses: list[dict[str, str]] = []

    listings = page.css("article.object")
    log.info("Marloes: %d listing elements found", len(listings))

    for listing in listings:
        try:
            link_els = listing.css("a")
            href = link_els[0].attrib.get("href", "") if link_els else ""
            if not href:
                continue
            url = make_absolute(href, "https://www.marloesmakelaars.nl")

            address = _first_text(listing, "h2")
            price = _first_text(listing, "h4")

            city = ""
            area = ""
            rooms = ""
            dts = listing.css("dt")
            dds = listing.css("dd")
            for dt, dd in zip(dts, dds):
                label = (dt.text or "").strip().lower()
                val = (dd.text or "").strip()
                if "plaats" in label:
                    city = val
                elif "oppervlakte" in label:
                    area = val
                elif "slaapkamer" in label or "kamer" in label:
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
            log.warning("Marloes: failed to parse a listing: %s", exc)

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
            if "," in title_text:
                address = title_text.rsplit(",", 1)[0].strip()

            price = _first_text(listing, "li.item-price")

            area_el = listing.css("li.h-area span.hz-figure")
            area = f"{(area_el[0].text or '').strip()} m²" if area_el else ""

            rooms = ""

            houses.append(
                {
                    "url": url,
                    "straatnaamHuisnummer": address or "Onbekend",
                    "plaats": "Delft",
                    "vraagprijs": price,
                    "oppervlakte": area,
                    "kamers": rooms,
                }
            )
        except Exception as exc:
            log.warning("Rent a Room: failed to parse a listing: %s", exc)

    return houses


# ---------------------------------------------------------------------------
# Frisia Makelaars parser (Move.nl platform)
# ---------------------------------------------------------------------------

def scrape_frisia(page) -> list[dict[str, str]]:
    dump_html(page, "frisia")
    houses: list[dict[str, str]] = []

    listings = page.css(".card.card--object")
    log.info("Frisia: %d listing elements found", len(listings))

    for listing in listings:
        try:
            link_els = listing.css("a.card__anchor")
            if not link_els:
                continue
            href = link_els[0].attrib.get("href", "")
            if not href:
                continue
            url = make_absolute(href, "https://frisiamakelaars.nl")

            if "/verkocht-verhuurd/" in url or "/wonen/koop/" in url:
                continue

            address = _first_text(listing, ".card--default__body h5", "h5")

            city = ""
            city_els = listing.css(".card--default__body small")
            if city_els:
                city_text = (city_els[0].text or "").strip()
                if "," in city_text:
                    city = city_text.split(",")[-1].strip()

            price_el = listing.css(".card--default__footer strong")
            price = (price_el[0].text or "").strip() if price_el else ""

            price_val = parse_price_euros(price)
            if price_val and price_val > MAX_PRICE:
                continue

            area = ""
            rooms = ""
            features = listing.css(".features li")
            for feat in features:
                small = feat.css("small")
                val = (small[0].text or "").strip() if small else ""
                if feat.css(".icon-livearea"):
                    area = val
                elif feat.css(".icon-door"):
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
            log.warning("Frisia: failed to parse a listing: %s", exc)

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
# Main loop
# ---------------------------------------------------------------------------

SITES = [
    ("Pararius", PARARIUS_URL, scrape_pararius),
    ("Funda", FUNDA_URL, scrape_funda),
    ("VBT Verhuurmakelaars", VBT_URL, scrape_vbt),
    ("Marloes Makelaars", MARLOES_URL, scrape_marloes),
    ("Hof van Delft", HOFVANDELFT_URL, scrape_hofvandelft),
    ("123Wonen", EENTWEEDRIEWONEN_URL, scrape_123wonen),
    ("Rotsvast", ROTSVAST_URL, scrape_rotsvast),
    ("Prinsenstad Makelaardij", PRINSENSTAD_URL, scrape_prinsenstad),
    ("Pactum Vastgoed", PACTUM_URL, scrape_pactum),
    ("VW Makelaars", VWMAKELAARS_URL, scrape_vwmakelaars),
    ("Rent a Room Delft", RENTAROOM_URL, scrape_rentaroom),
    ("Frisia Makelaars", FRISIA_URL, scrape_frisia),
    ("Oude Delft", OUDEDELFT_URL, scrape_oudedelft),
    ("PSG Wonen", PSGWONEN_URL, scrape_psgwonen),
    ("Van Gulden Makelaardij", VANGULDEN_URL, scrape_vangulden),
    ("ZO Makelaars", ZOMAKELAARS_URL, scrape_zomakelaars),
    ("ikwilhuren.nu", IKWILHUREN_URL, scrape_ikwilhuren),
    ("070 Wonen", ZEVENTIGWONEN_URL, scrape_070wonen),
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
        _record_fetch_failure("timeout")
        raise
    except Exception as exc:
        # Playwright/Camoufox sometimes wedges the browser; once that happens
        # every subsequent fetch hangs. Count these alongside timeouts so the
        # self-heal threshold can trip from either symptom.
        if "Page crashed" in str(exc):
            _record_fetch_failure("page crashed")
        raise
    _consecutive_fetch_failures = 0
    return page


def _record_fetch_failure(reason: str) -> None:
    global _consecutive_fetch_failures
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
        sys.exit(1)


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
            log.warning("%s page %d timed out after %ds, skipping", name, page_num, FETCH_TIMEOUT)
            send_telegram_alert(f"⚠️ <b>{name}</b> page {page_num} timed out after {FETCH_TIMEOUT}s — skipped")
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
                page = _fetch_with_timeout(url)
                all_houses = parser(page)
                houses = [h for h in all_houses if h["url"] not in existing_urls]
                log.info("%s: %d scraped, %d new", name, len(all_houses), len(houses))

            if houses:
                save_houses(conn, houses)
                all_new.extend(houses)
                existing_urls.update(h["url"] for h in houses)
        except TimeoutError:
            log.warning("%s timed out after %ds, skipping", name, FETCH_TIMEOUT)
            send_telegram_alert(f"⚠️ <b>{name}</b> timed out after {FETCH_TIMEOUT}s — skipped")
        except Exception as exc:
            log.error("%s scrape failed: %s", name, exc, exc_info=True)

    if all_new:
        send_telegram(all_new)

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

    while True:
        try:
            new = run_cycle()
            log.info("Cycle done — %d new listing(s)", len(new))
        except Exception as exc:
            log.error("Cycle failed: %s", exc, exc_info=True)
        time.sleep(CHECK_INTERVAL)


if __name__ == "__main__":
    main()
