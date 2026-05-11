import json
import logging
import os
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

PARARIUS_URL = "https://www.pararius.nl/huurwoningen/delft/0-1500"
FUNDA_URL = (
    "https://www.funda.nl/zoeken/huur"
    "?selected_area=%5B%22delft%22%5D&price=%220-1500%22"
)


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
            url = (
                f"https://www.pararius.nl{href}"
                if href.startswith("/")
                else href
            )

            subtitle = listing.css(".listing-search-item__sub-title")
            city = (subtitle[0].text or "").strip() if subtitle else "Delft"

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
            url = (
                f"https://www.funda.nl{href}"
                if href.startswith("/")
                else href
            )

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
# Main loop
# ---------------------------------------------------------------------------

SITES = [
    ("Pararius", PARARIUS_URL, scrape_pararius),
    ("Funda", FUNDA_URL, scrape_funda),
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
        "Starting Pararius/Funda sidecar  db=%s  interval=%ds  debug=%s",
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
