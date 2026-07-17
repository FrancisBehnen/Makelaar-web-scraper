"""Detect rental listings that are no longer available and clean up after them.

Each cycle the responder re-fetches a small batch of previously-notified,
still-"available" listings over plain HTTP. A listing counts as *gone* when the
page 404/410s or its body carries a Dutch rented-out status phrase. For those we
delete the original Telegram notification message(s) and send one short summary
of everything removed this cycle (mirrors the sales-sidecar mechanism).

The shared machinery (page-scoped detection, the round-robin recheck loop and
the replaceable summary) lives in ``shared.lifecycle``; this module supplies the
rental specifics: the cookie-safe HTTP fetch (huurstunt session), the rental
status vocabulary, and the ``responses``-table accessors.

Detection is deliberately conservative: a listing wrongly deleted is worse than
one lingering. Server-rendered detail pages routinely embed "gerelateerd
aanbod" / "recent verhuurd" carousels whose *other* cards carry badges like
"verhuurd onder voorbehoud" or "onder optie". A whole-body regex would treat a
still-live listing as gone the moment such a neighbour appears, so detection is
page-scoped (see ``shared.lifecycle.reads_gone``):

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
import urllib.request
from urllib.parse import urlparse

import config
import db
import tg
from letter import escape_html as esc
from shared import lifecycle

log = logging.getLogger("responder")

_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
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
# are only trusted inside the page's header region.
_BADGE_STATUS_RE = re.compile(
    r"verhuurd onder voorbehoud|onder optie",
    re.IGNORECASE,
)

_GONE_SUMMARY_KV = "gone_summary_ids"
_SUMMARY_TITLE = (
    "\U0001f5d1 <b>{count} {word} niet meer beschikbaar — "
    "bericht(en) verwijderd</b>"
)


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


def _fetch_body(url: str) -> bytes:
    """Fetch ``url`` through the cookie-safe opener; may raise HTTPError."""
    req = urllib.request.Request(url, headers=_headers(url))
    with _fetch(req) as resp:
        return resp.read()


def is_gone(url: str) -> bool:
    """Return True when the listing page is a 404/410 or reads as rented-out.

    Any other outcome (200 that still looks live, redirect, transient network
    error surfaced as a non-HTTP exception) returns False. Network exceptions
    propagate so the caller can skip without marking the listing gone.
    """
    return lifecycle.is_gone(
        url,
        fetch=_fetch_body,
        page_status_re=_PAGE_STATUS_RE,
        badge_status_re=_BADGE_STATUS_RE,
    )


def _gone_marker_text(addr: str, url: str) -> str:
    """In-place replacement text for a gone listing whose original notification
    can't be deleted.

    Telegram only lets a bot delete its own messages for 48 hours; a rental
    that goes gone has often been on the market longer than that, so the
    delete fails and the live notification would otherwise linger.
    ``editMessageText`` has no such time limit, so we edit the card to mark it
    gone instead — the group never shows a stale "available" listing.
    """
    return (
        "\U0001f6d1 <b>Niet meer beschikbaar / verhuurd</b>\n"
        f"<s>{esc(addr or url)}</s>\n"
        f"{esc(url)}"
    )


def _delete_listing_messages(row) -> tuple[str, str] | None:
    """Delete the Telegram message(s) for a gone listing and mark it gone.

    When ``deleteMessage`` fails (e.g. the Telegram 48h delete window has
    passed), fall back to editing the message in place so the notification
    never lingers as a stale live listing.

    Returns an ``(address, url)`` pair (for the linked summary), or None if
    there is nothing to do.
    """
    addr = row["straatnaamHuisnummer"] or row["url"]
    marker = _gone_marker_text(addr, row["url"])
    for chat_id, message_id in json.loads(row["tg_message_ids"] or "{}").items():
        if not tg.delete_message(str(chat_id), message_id):
            tg.edit_text(str(chat_id), message_id, marker)
    db.mark_listing_gone(row["id"])
    log.info("Listing gone, deleted TG message(s): %s (%s)", addr, row["url"])
    return addr, row["url"]


def recheck_delisted() -> list[tuple[str, str]]:
    """Re-check a batch of available listings; return (address, url) pairs
    for the listings newly removed this cycle."""
    rows = db.available_listings(config.RECHECK_BATCH_SIZE)
    return lifecycle.run_recheck(
        rows,
        # Advance the round-robin cursor first, so a persistently failing URL
        # never blocks the front of the queue.
        mark_checked=lambda row: db.touch_listing_checked(row["id"]),
        gone=lambda row: is_gone(row["url"]),
        on_gone=_delete_listing_messages,
        # Surface (don't swallow) recheck fetch errors so a timeout is visible in
        # the logs instead of a silent cursor advance.
        on_error=lambda row, exc: log.warning(
            "Recheck fetch of %s failed: %s", row["url"], exc
        ),
    )


def _edit_summary(message_ids: list[dict], text: str) -> bool:
    """Edit every {chat_id, message_id} of the live summary. True if all edits
    succeeded (a failure triggers the send-fresh fallback in the engine)."""
    if not message_ids:
        return False
    return all(
        tg.edit_text(str(entry["chat_id"]), entry["message_id"], text)
        for entry in message_ids
    )


def _send_summary(text: str) -> list[dict]:
    """Broadcast the summary SILENTLY (no push) and normalise the returned
    {chat_id: message_id} dict to the state's list-of-dicts shape."""
    sent = tg.broadcast(text, disable_notification=True)
    return [{"chat_id": chat_id, "message_id": mid} for chat_id, mid in sent.items()]


def send_gone_summary(listings: list[tuple[str, str]]) -> None:
    """Update the persistent, accumulating gone-summary (edited in place).

    ``listings`` is a list of ``(address, url)`` pairs; each bullet links the
    address to its listing URL. The summary is a single message per local day
    that is edited (silently) as more listings go gone, so only genuinely new
    listings ever push. State survives restarts via the ``kv`` table.
    """

    def build_text(entries: list[tuple[str, str]]) -> str:
        return lifecycle.build_summary_text(
            entries, title_template=_SUMMARY_TITLE, escape=esc
        )

    def load_state() -> dict | None:
        raw = db.kv_get(_GONE_SUMMARY_KV)
        return json.loads(raw) if raw else None

    lifecycle.upsert_accumulating_summary(
        listings,
        load_state=load_state,
        save_state=lambda state: db.kv_set(_GONE_SUMMARY_KV, json.dumps(state)),
        edit=_edit_summary,
        send=_send_summary,
        delete=lambda message_ids: [
            tg.delete_message(str(e["chat_id"]), e["message_id"])
            for e in message_ids
        ],
        build_text=build_text,
    )
