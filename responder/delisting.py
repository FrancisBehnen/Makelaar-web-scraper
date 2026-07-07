"""Detect rental listings that are no longer available and clean up after them.

Each cycle the responder re-fetches a small batch of previously-notified,
still-"available" listings over plain HTTP. A listing counts as *gone* when the
page 404/410s or its body carries a Dutch rented-out status phrase. For those we
delete the original Telegram notification message(s) and send one short summary
of everything removed this cycle (mirrors the sales-sidecar mechanism).

Detection is deliberately conservative: a listing wrongly deleted is worse than
one lingering. Server-rendered detail pages routinely embed "gerelateerd
aanbod" / "recent verhuurd" carousels whose *other* cards carry badges like
"verhuurd onder voorbehoud" or "onder optie". A whole-body regex would treat a
still-live listing as gone the moment such a neighbour appears, so detection is
page-scoped instead:

  * sidebar/footer carousels are stripped before matching;
  * unambiguous "this listing" phrases ("deze woning is verhuurd",
    "status: verhuurd", …) are trusted anywhere in the remaining body;
  * bare status badges ("verhuurd onder voorbehoud", "onder optie") — which
    also appear on neighbouring cards — are trusted only inside the page's own
    header region (around the listing's <h1>).

A missed detection is acceptable; a false deletion is not.
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

# Sidebar / footer carousels ("gerelateerd aanbod", "recent verhuurd") hold
# OTHER listings' cards. Their status badges must never be read as the primary
# listing's status, so these blocks are removed before matching.
_SIDEBAR_RE = re.compile(
    r"<(aside|footer)\b[^>]*>.*?</\1>",
    re.IGNORECASE | re.DOTALL,
)

# Unambiguous, page-scoped "this listing is rented / withdrawn" phrases. They
# describe THE primary listing and never appear on a neighbouring card (which
# reads "Marktplein 4 — verhuurd"), so they are trusted anywhere in the body.
_PAGE_STATUS_RE = re.compile(
    r"deze woning is (?:inmiddels |per direct )?verhuurd"
    r"|deze woning is onder optie"
    r"|woning is verhuurd"
    r"|status:\s*verhuurd"
    r"|niet meer beschikbaar",
    re.IGNORECASE,
)

# Standalone status badges. These also appear on other listings' cards, so they
# are only trusted inside the page's header region (see _header_region).
_BADGE_STATUS_RE = re.compile(
    r"verhuurd onder voorbehoud|onder optie",
    re.IGNORECASE,
)

# Window (chars) around the listing's <h1> in which a bare status badge counts.
_HEADER_REGION = 1500

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


class _CookieSafeRedirectHandler(urllib.request.HTTPRedirectHandler):
    """Drop the Cookie header when a redirect crosses to a different host.

    urllib copies request headers (including Cookie) onto the redirected
    request even cross-host, which would leak the huurstunt session cookie to a
    third-party host. Same-host redirects (http->https, canonical URL) keep the
    cookie so huurstunt detail pages still resolve.
    """

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        new = super().redirect_request(req, fp, code, msg, headers, newurl)
        if new is None:
            return None
        if urlparse(req.full_url).hostname != urlparse(newurl).hostname:
            new.remove_header("Cookie")
        return new


_OPENER = urllib.request.build_opener(_CookieSafeRedirectHandler)


def _fetch(req: urllib.request.Request):
    """Open ``req`` through the cookie-safe opener (test seam)."""
    return _OPENER.open(req, timeout=15)


def _header_region(body: str) -> str:
    """Return the slice around the listing's <h1> where a badge is trusted."""
    m = re.search(r"<h1\b", body, re.IGNORECASE)
    start = m.start() if m else 0
    return body[start : start + _HEADER_REGION]


def _reads_gone(html: str) -> bool:
    """Page-scoped rented-out detection (see module docstring)."""
    body = _SIDEBAR_RE.sub(" ", html)
    if _PAGE_STATUS_RE.search(body):
        return True
    return bool(_BADGE_STATUS_RE.search(_header_region(body)))


def is_gone(url: str) -> bool:
    """Return True when the listing page is a 404/410 or reads as rented-out.

    Any other outcome (200 that still looks live, redirect, transient network
    error surfaced as a non-HTTP exception) returns False. Network exceptions
    propagate so the caller can skip without marking the listing gone.
    """
    req = urllib.request.Request(url, headers=_headers(url))
    try:
        with _fetch(req) as resp:
            body = resp.read()
    except urllib.error.HTTPError as exc:
        return exc.code in _GONE_HTTP_CODES
    return _reads_gone(body.decode("utf-8", errors="ignore"))


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
