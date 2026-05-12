import json
import logging
import os
import re
import sqlite3
import time
import urllib.request
from pathlib import Path

from scrapling.fetchers import StealthyFetcher

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
VWMAKELAARS_URL = "https://delft.vwmakelaars.nl/aanbod/woningaanbod/"
RENTAROOM_URL = "https://rent-a-room-delft.nl/grid-default/"
FRISIA_URL = (
    "https://frisiamakelaars.nl/wonen/aanbod"
    "?buy_rent=rent&rent_price=-1500&distance=5"
    "&search=delft&order_by=created_at-desc&page=1"
)
OUDEDELFT_URL = "https://oudedelft.com/huur-2/"

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

    listings = _find_elements(
        page,
        ".residence-card",
        ".property-card",
        ".woning-card",
        "a[href*='/woning/']",
    )
    log.info("VBT: %d listing elements found", len(listings))

    for listing in listings:
        try:
            href = listing.attrib.get("href", "")
            if not href:
                link_els = listing.css("a[href*='/woning/']")
                if link_els:
                    href = link_els[0].attrib.get("href", "")
            if not href:
                continue
            url = make_absolute(href, "https://vbtverhuurmakelaars.nl")

            all_text = (listing.get_all_text() or "").strip()

            price = ""
            area = ""
            rooms = ""
            city = ""
            address = ""

            price_match = re.search(r"€\s*[\d.,]+,-?", all_text)
            if price_match:
                price = price_match.group(0).strip()

            area_match = re.search(r"(\d+)\s*m[²2]", all_text)
            if area_match:
                area = f"{area_match.group(1)} m²"

            rooms_match = re.search(
                r"(\d+)\s*[Kk]amer", all_text
            )
            if rooms_match:
                rooms = rooms_match.group(0).strip()

            city_el = listing.css(".city, .plaats, .location-city")
            if city_el:
                city = (city_el[0].text or "").strip()
            else:
                for known_city in DELFT_AREA_CITIES:
                    if known_city.lower() in all_text.lower():
                        city = known_city.title()
                        break

            address_el = listing.css(
                ".address, .street, .straatnaam, h2, h3"
            )
            if address_el:
                address = (address_el[0].text or "").strip()

            if not address and "/woning/" in href:
                slug = href.split("/woning/")[-1].rstrip("/")
                parts = slug.rsplit("-", 1)
                if len(parts) >= 1:
                    address = parts[0].replace("-", " ").title()

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

    listings = _find_elements(
        page,
        ".property-item",
        ".woning-item",
        ".object-item",
        "a[href*='/woning/']",
    )
    log.info("Marloes: %d listing elements found", len(listings))

    for listing in listings:
        try:
            href = listing.attrib.get("href", "")
            if not href:
                link_els = listing.css("a[href*='/woning/']")
                if link_els:
                    href = link_els[0].attrib.get("href", "")
            if not href:
                continue
            url = make_absolute(href, "https://www.marloesmakelaars.nl")

            address = _first_text(listing, "h3", "h2", ".address", ".title")

            price = _first_text(listing, "h4", ".price", ".vraagprijs")
            if not price:
                all_text = listing.get_all_text() or ""
                price_match = re.search(r"€\s*[\d.,]+", all_text)
                if price_match:
                    price = price_match.group(0).strip()

            city = ""
            area = ""
            rooms = ""
            all_text = listing.get_all_text() or ""

            city_match = re.search(
                r"(?:Plaats|Stad|City)[:\s]*(\w[\w\s]*)", all_text
            )
            if city_match:
                city = city_match.group(1).strip()
            elif "DELFT" in all_text.upper():
                city = "Delft"

            area_match = re.search(r"(\d+)\s*m[²2]", all_text)
            if area_match:
                area = f"{area_match.group(1)} m²"

            rooms_match = re.search(
                r"(?:Slaapkamer|Kamer)s?\s*[:\s]*(\d+)", all_text,
                re.IGNORECASE,
            )
            if rooms_match:
                rooms = f"{rooms_match.group(1)} kamers"
            else:
                rooms_match2 = re.search(
                    r"(\d+)\s*(?:slaap)?kamer", all_text, re.IGNORECASE
                )
                if rooms_match2:
                    rooms = rooms_match2.group(0).strip()

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

def scrape_hofvandelft(page) -> list[dict[str, str]]:
    dump_html(page, "hofvandelft")
    houses: list[dict[str, str]] = []

    listings = _find_elements(
        page,
        ".aanbodEntry",
        ".aanbodEntryWrap",
        ".object_item",
        ".objectcontainer",
        "a[href*='/object/']",
        ".property-card",
        ".woning",
    )
    log.info("Hof van Delft: %d listing elements found", len(listings))

    for listing in listings:
        try:
            href = listing.attrib.get("href", "")
            if not href:
                link_els = listing.css("a")
                for el in link_els:
                    h = el.attrib.get("href", "")
                    if h and "/object/" in h or "/woning/" in h:
                        href = h
                        break
                if not href and link_els:
                    href = link_els[0].attrib.get("href", "")
            if not href:
                continue
            url = make_absolute(href, "https://www.hofvandelft.nl")

            address = _first_text(
                listing,
                ".street-address", ".adres", ".objectTitle",
                ".straatnaam", "h2", "h3",
            )
            city = _first_text(
                listing,
                ".locality", ".plaats", ".city",
            )

            all_text = listing.get_all_text() or ""

            price = _first_text(listing, ".price", ".vraagprijs", ".huurprijs")
            if not price:
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
            log.warning("Hof van Delft: failed to parse a listing: %s", exc)

    return houses


# ---------------------------------------------------------------------------
# 123Wonen parser
# ---------------------------------------------------------------------------

def scrape_123wonen(page) -> list[dict[str, str]]:
    dump_html(page, "123wonen")
    houses: list[dict[str, str]] = []

    listings = _find_elements(
        page,
        ".property-card",
        ".object-card",
        ".search-result",
        "a[href*='/huur/']",
        ".listing-item",
    )
    log.info("123Wonen: %d listing elements found", len(listings))

    for listing in listings:
        try:
            href = listing.attrib.get("href", "")
            if not href:
                link_els = listing.css("a[href*='/huur/']")
                if not link_els:
                    link_els = listing.css("a")
                if link_els:
                    href = link_els[0].attrib.get("href", "")
            if not href:
                continue
            url = make_absolute(href, "https://www.123wonen.nl")

            all_text = listing.get_all_text() or ""

            address = _first_text(
                listing, ".street", ".address", "h2", "h3", ".title"
            )

            city = ""
            location_match = re.search(
                r"(Delft|Delfgauw|Den Hoorn|Rijswijk|Schipluiden|Nootdorp|Pijnacker)",
                all_text,
                re.IGNORECASE,
            )
            if location_match:
                city = location_match.group(1).title()

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
            log.warning("123Wonen: failed to parse a listing: %s", exc)

    return houses


# ---------------------------------------------------------------------------
# Rotsvast parser
# ---------------------------------------------------------------------------

def scrape_rotsvast(page) -> list[dict[str, str]]:
    dump_html(page, "rotsvast")
    houses: list[dict[str, str]] = []

    listings = _find_elements(
        page,
        ".property-card",
        ".object-card",
        "a[href*='/huren/']",
        ".listing",
    )
    log.info("Rotsvast: %d listing elements found", len(listings))

    for listing in listings:
        try:
            href = listing.attrib.get("href", "")
            if not href:
                link_els = listing.css("a[href*='/huren/']")
                if link_els:
                    href = link_els[0].attrib.get("href", "")
            if not href or "/huren/" not in href:
                continue
            url = make_absolute(href, "https://www.rotsvast.nl")

            all_text = listing.get_all_text() or ""
            children = listing.css("div, span, p")

            city = ""
            address = ""
            price = ""

            for child in children:
                txt = (child.text or "").strip()
                if not txt:
                    continue
                if "€" in txt:
                    price = txt
                elif txt in (
                    "Delft", "Delfgauw", "Den Hoorn", "Rijswijk",
                    "Schipluiden", "Nootdorp", "Pijnacker",
                ):
                    city = txt
                elif (
                    not address
                    and "€" not in txt
                    and "m²" not in txt
                    and not txt.startswith("Direct")
                    and txt not in ("Kaal", "Gestoffeerd", "Gemeubileerd")
                ):
                    address = txt

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
        ".object-item",
        ".property-card",
        ".aanbodEntry",
        "[class*='object']",
        "a[href*='/object/']",
    )
    log.info("Prinsenstad: %d listing elements found", len(listings))

    for listing in listings:
        try:
            href = listing.attrib.get("href", "")
            if not href:
                link_els = listing.css("a")
                if link_els:
                    href = link_els[0].attrib.get("href", "")
            if not href:
                continue
            url = make_absolute(href, "https://prinsenstadmakelaardij.nl")

            all_text = listing.get_all_text() or ""
            address = _first_text(
                listing, ".address", ".street", "h2", "h3", ".title"
            )
            city = _first_text(listing, ".city", ".plaats", ".locality")

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
        ".property-card",
        ".woning-card",
        "[class*='property']",
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
    dump_html(page, "vwmakelaars")
    houses: list[dict[str, str]] = []

    listings = _find_elements(
        page,
        ".aanbodEntry",
        ".aanbodEntryWrap",
        ".objectcontainer",
        ".object_item",
        "a[href*='/object/']",
        ".property-card",
    )
    log.info("VW Makelaars: %d listing elements found", len(listings))

    for listing in listings:
        try:
            href = listing.attrib.get("href", "")
            if not href:
                link_els = listing.css("a")
                if link_els:
                    href = link_els[0].attrib.get("href", "")
            if not href:
                continue
            url = make_absolute(href, "https://delft.vwmakelaars.nl")

            all_text = listing.get_all_text() or ""
            address = _first_text(
                listing,
                ".street-address", ".adres", ".objectTitle",
                ".straatnaam", "h2", "h3",
            )
            city = _first_text(listing, ".locality", ".plaats", ".city")

            price = _first_text(listing, ".price", ".vraagprijs", ".huurprijs")
            if not price:
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
            log.warning("VW Makelaars: failed to parse a listing: %s", exc)

    return houses


# ---------------------------------------------------------------------------
# Rent a Room Delft parser (WordPress / Estatik)
# ---------------------------------------------------------------------------

def scrape_rentaroom(page) -> list[dict[str, str]]:
    dump_html(page, "rentaroom")
    houses: list[dict[str, str]] = []

    listings = _find_elements(
        page,
        ".es-listing",
        ".property-card",
        ".es-property",
        "a[href*='/property/']",
        ".listing-item",
    )
    log.info("Rent a Room: %d listing elements found", len(listings))

    for listing in listings:
        try:
            href = listing.attrib.get("href", "")
            if not href:
                link_els = listing.css("a[href*='/property/']")
                if not link_els:
                    link_els = listing.css("a")
                if link_els:
                    href = link_els[0].attrib.get("href", "")
            if not href:
                continue
            url = make_absolute(href, "https://rent-a-room-delft.nl")

            all_text = listing.get_all_text() or ""
            address = _first_text(listing, "h2", "h3", ".title", ".address")

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

    listings = _find_elements(
        page,
        ".property-card",
        ".object-card",
        ".woning-item",
        "a[href*='/wonen/']",
        ".listing-item",
        ".search-result",
    )
    log.info("Frisia: %d listing elements found", len(listings))

    for listing in listings:
        try:
            href = listing.attrib.get("href", "")
            if not href:
                link_els = listing.css("a[href*='/wonen/']")
                if not link_els:
                    link_els = listing.css("a")
                if link_els:
                    href = link_els[0].attrib.get("href", "")
            if not href:
                continue
            url = make_absolute(href, "https://frisiamakelaars.nl")

            all_text = listing.get_all_text() or ""
            address = _first_text(
                listing, ".address", ".street", "h2", "h3", ".title"
            )
            city = _first_text(listing, ".city", ".plaats", ".location")

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
            log.warning("Frisia: failed to parse a listing: %s", exc)

    return houses


# ---------------------------------------------------------------------------
# Oude Delft parser (WordPress + JS plugin)
# ---------------------------------------------------------------------------

def scrape_oudedelft(page) -> list[dict[str, str]]:
    dump_html(page, "oudedelft")
    houses: list[dict[str, str]] = []

    listings = _find_elements(
        page,
        ".property-card",
        ".woning-item",
        ".object-item",
        "a[href*='/woning/']",
        "a[href*='/huur/']",
        ".listing-item",
        ".es-listing",
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
]


def run_cycle():
    conn = init_db()
    existing_urls = get_existing_urls(conn)
    all_new = []

    for name, url, parser in SITES:
        try:
            log.info("Fetching %s ...", name)
            page = StealthyFetcher.fetch(
                url,
                headless=True,
                solve_cloudflare=True,
                network_idle=True,
            )
            houses = parser(page)
            new_houses = [h for h in houses if h["url"] not in existing_urls]
            log.info("%s: %d scraped, %d new", name, len(houses), len(new_houses))

            if new_houses:
                save_houses(conn, new_houses)
                all_new.extend(new_houses)
                existing_urls.update(h["url"] for h in new_houses)
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
