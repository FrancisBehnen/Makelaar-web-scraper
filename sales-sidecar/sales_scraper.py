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
PARARIUS_KOOP_URL = (
    "https://www.pararius.nl/koopwoningen/delft/0-270000/page-{page}"
)
ZOMAKELAARS_KOOP_URL = (
    "https://www.zomakelaars.nl/aanbod/woningaanbod/Delft/koop/"
    "provincie-Zuid-Holland/"
)
VWMAKELAARS_KOOP_URL = "https://delft.vwmakelaars.nl/aanbod/woningaanbod/koop/"

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


def passes_filters(h: dict[str, str]) -> bool:
    """Keep koop apartments in Delft, <= EUR 270.000, >= 2 rooms, no studios.

    - Price must be parseable and <= MAX_PRICE (unknown price -> exclude).
    - City must be Delft.
    - Rooms, when known, must be >= 2 (unknown rooms -> keep).
    - "studio" anywhere in the address -> exclude.
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

    if "studio" in (h.get("straatnaamHuisnummer", "") or "").lower():
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
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS sales (
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
    return (
        (text or "")
        .replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
    )


def _send(chat_ids_raw: str, text: str) -> None:
    if not TELEGRAM_BOT_TOKEN:
        return
    chat_ids = [c.strip() for c in (chat_ids_raw or "").split(",") if c.strip()]
    if not chat_ids:
        return
    api_url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    for chat_id in chat_ids:
        body = json.dumps(
            {
                "chat_id": chat_id,
                "text": text,
                "parse_mode": "HTML",
                "disable_web_page_preview": True,
            }
        ).encode()
        req = urllib.request.Request(
            api_url, data=body, headers={"Content-Type": "application/json"}
        )
        try:
            urllib.request.urlopen(req, timeout=10)
        except Exception as exc:
            log.error("Telegram send to %s failed: %s", chat_id, exc)


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


def notify_new_listing(h: dict[str, str]) -> None:
    _send(TELEGRAM_SALES_CHAT_IDS, _listing_text(h))


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
            address = " ".join(
                (s.text or "").strip() for s in addr_spans
            ).strip()

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
            log.warning("Funda koop: failed to parse a listing: %s", exc)

    return houses


def _scrape_realworks_koop(
    page, base_url: str, site_name: str
) -> list[dict[str, str]]:
    """Realworks-platform parser with an inverted status gate: keep koop URLs,
    skip /huur/ and /verkocht/. City check uses is_delft_city.
    """
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

            if "/huur/" in url or "/verkocht/" in url:
                continue

            address = _first_text(listing, "h3.street-address")
            city = _first_text(listing, "span.locality")

            if city and not is_delft_city(city):
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


def scrape_zomakelaars_koop(page) -> list[dict[str, str]]:
    return _scrape_realworks_koop(
        page, "https://www.zomakelaars.nl", "ZO Makelaars koop"
    )


def scrape_vwmakelaars_koop(page) -> list[dict[str, str]]:
    return _scrape_realworks_koop(
        page, "https://delft.vwmakelaars.nl", "VW Makelaars koop"
    )


SITES = [
    ("Pararius koop", PARARIUS_KOOP_URL, scrape_pararius_koop),
    ("Funda koop", FUNDA_KOOP_URL, scrape_funda_koop),
    ("ZO Makelaars koop", ZOMAKELAARS_KOOP_URL, scrape_zomakelaars_koop),
    ("VW Makelaars koop", VWMAKELAARS_KOOP_URL, scrape_vwmakelaars_koop),
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
                isinstance(first_exc, TimeoutError)
                or _is_browser_crash(first_exc)
            ):
                raise
            label = (
                "timed out"
                if isinstance(first_exc, TimeoutError)
                else "browser crash"
            )
            log.warning(
                "%s page %d %s, retrying once ...", name, page_num, label
            )
            try:
                page = _fetch_with_timeout(url)
            except Exception as retry_exc:
                if not (
                    isinstance(retry_exc, TimeoutError)
                    or _is_browser_crash(retry_exc)
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
                    f"⚠️ <b>{name}</b> page {page_num} failed ({label}) — "
                    f"skipped",
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
        notify_new_listing(h)
        notified += 1
    return notified


def run_cycle():
    conn = init_db()
    existing_urls = get_existing_urls(conn)
    seeding = table_is_empty(conn)
    if seeding:
        log.info("Sales table empty — seeding without notifications")

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
                    if not (
                        isinstance(exc, TimeoutError) or _is_browser_crash(exc)
                    ):
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
            notified_total += process_houses(
                conn, matched, existing_urls, seeding
            )
        except TimeoutError:
            log.warning("%s timed out after %ds, skipping", name, FETCH_TIMEOUT)
            send_throttled_timeout_alert(
                name,
                f"⚠️ <b>{name}</b> timed out after {FETCH_TIMEOUT}s — skipped",
            )
        except Exception as exc:
            log.error("%s scrape failed: %s", name, exc, exc_info=True)

    conn.close()
    return notified_total, counts


def main():
    if "--once" in sys.argv:
        notified, counts = run_cycle()
        log.info("Single cycle done — matches=%s notified=%d", counts, notified)
        return

    log.info(
        "Starting sales scraper (%d sources)  db=%s  interval=%ds  debug=%s",
        len(SITES),
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
            log.info(
                "Cycle done — matches=%s notified=%d", counts, notified
            )
        except Exception as exc:
            log.error("Cycle failed: %s", exc, exc_info=True)
        time.sleep(CHECK_INTERVAL)


if __name__ == "__main__":
    main()
