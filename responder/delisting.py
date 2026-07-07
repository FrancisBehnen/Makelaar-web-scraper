"""Detect rental listings that are no longer available and clean up after them.

Each cycle the responder re-fetches a small batch of previously-notified,
still-"available" listings over plain HTTP. A listing counts as *gone* when the
page 404/410s or its body carries a Dutch rented-out status phrase. For those we
delete the original Telegram notification message(s) and send one short summary
of everything removed this cycle (mirrors the sales-sidecar mechanism).

Detection is deliberately conservative: a listing wrongly deleted is worse than
one lingering, so the status regex matches specific "this listing is rented"
phrases rather than a bare occurrence of "verhuurd" (which routinely appears in
"recently rented" widgets listing *other* properties).
"""

import json
import logging
import re
import urllib.error
import urllib.request
from urllib.parse import urlparse

import config
import db
import tg
from letter import escape_html as esc

log = logging.getLogger("responder")

_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
)

# HTTP statuses that mean the listing page is gone for good.
_GONE_HTTP_CODES = frozenset({404, 410})

# Conservative "this listing is rented / withdrawn" phrases. Bare "verhuurd" is
# intentionally excluded — it shows up in unrelated card lists on live pages.
_GONE_STATUS_RE = re.compile(
    r"verhuurd onder voorbehoud"
    r"|onder optie"
    r"|niet meer beschikbaar"
    r"|deze woning is (?:inmiddels |per direct )?verhuurd"
    r"|woning is verhuurd"
    r"|status:\s*verhuurd",
    re.IGNORECASE,
)

_GONE_SUMMARY_KV = "gone_summary_ids"


def _registrable_domain(url: str) -> str:
    host = (urlparse(url).hostname or "").lower().removeprefix("www.")
    parts = host.split(".")
    return ".".join(parts[-2:]) if len(parts) >= 2 else host


def _headers(url: str) -> dict[str, str]:
    headers = {"User-Agent": _UA}
    if config.HUURSTUNT_COOKIE and _registrable_domain(url) == "huurstunt.nl":
        headers["Cookie"] = config.HUURSTUNT_COOKIE
    return headers


def is_gone(url: str) -> bool:
    """Return True when the listing page is a 404/410 or reads as rented-out.

    Any other outcome (200 that still looks live, redirect, transient network
    error surfaced as a non-HTTP exception) returns False. Network exceptions
    propagate so the caller can skip without marking the listing gone.
    """
    req = urllib.request.Request(url, headers=_headers(url))
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            body = resp.read()
    except urllib.error.HTTPError as exc:
        return exc.code in _GONE_HTTP_CODES
    text = body.decode("utf-8", errors="ignore")
    return bool(_GONE_STATUS_RE.search(text))


def _delete_listing_messages(row) -> str | None:
    """Delete the Telegram message(s) for a gone listing and mark it gone.

    Returns the address string (for the summary), or None if nothing to do.
    """
    addr = row["straatnaamHuisnummer"] or row["url"]
    for chat_id, message_id in json.loads(row["tg_message_ids"] or "{}").items():
        tg.delete_message(str(chat_id), message_id)
    db.mark_listing_gone(row["id"])
    log.info("Listing gone, deleted TG message(s): %s (%s)", addr, row["url"])
    return addr


def recheck_delisted() -> list[str]:
    """Re-check a batch of available listings; return addresses newly removed."""
    rows = db.available_listings(config.RECHECK_BATCH_SIZE)
    removed: list[str] = []
    for row in rows:
        # Advance the round-robin cursor first, so a persistently failing URL
        # never blocks the front of the queue.
        db.touch_listing_checked(row["id"])
        try:
            gone = is_gone(row["url"])
        except Exception as exc:
            log.debug("Recheck fetch of %s failed: %s", row["url"], exc)
            continue
        if gone:
            addr = _delete_listing_messages(row)
            if addr is not None:
                removed.append(addr)
    return removed


def send_gone_summary(addresses: list[str]) -> None:
    """Send one summary of removed listings, replacing the previous summary."""
    prev = db.kv_get(_GONE_SUMMARY_KV)
    if prev:
        for chat_id, message_id in json.loads(prev).items():
            tg.delete_message(str(chat_id), message_id)

    count = len(addresses)
    word = "woning" if count == 1 else "woningen"
    listing_lines = "\n".join(f"• {esc(a)}" for a in addresses)
    text = (
        f"\U0001f5d1 <b>{count} {word} niet meer beschikbaar — "
        f"bericht(en) verwijderd</b>\n\n{listing_lines}"
    )
    message_ids = tg.broadcast(text)
    db.kv_set(_GONE_SUMMARY_KV, json.dumps(message_ids))
