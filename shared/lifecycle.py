"""Shared listing-lifecycle machinery for the responder and the sales-sidecar.

Both services converge on the same shape once a listing has been notified:

    notify -> track Telegram message ids -> re-check a batch each cycle ->
    on a status transition (rented / sold) delete the notification message(s)
    and send one batched, replaceable summary of everything removed this cycle.

Only the *vocabulary* differs (verhuurd / onder optie vs verkocht / onder bod)
and the *plumbing* (which DB, which HTTP fetch, which Telegram sender). This
module owns the shared, behaviour-defining logic and takes those specifics as
parameters:

* :func:`reads_gone` / :func:`is_gone` — page-scoped, conservative
  "this listing is gone" detection. A wrongly deleted listing is worse than one
  lingering, so sidebar/footer carousels are stripped before matching,
  unambiguous page-status phrases are trusted anywhere, and bare status badges
  (which also appear on neighbouring "recently rented/sold" cards) are trusted
  only inside the page's own header region.
* :func:`run_recheck` — the round-robin recheck loop: the caller supplies a
  batch of the least-recently-checked available listings; each item's check
  cursor is advanced *before* the fetch, so a persistently failing URL never
  blocks the front of the queue.
* :func:`send_replaceable_summary` — build + send one summary, deleting the
  previous one first.
"""

import re
import urllib.error
from collections.abc import Callable, Iterable
from typing import Any

# HTTP statuses that mean the listing page is gone for good.
DEFAULT_GONE_HTTP_CODES = frozenset({404, 410})

# Window (chars) around the listing's <h1> in which a bare status badge counts.
DEFAULT_HEADER_REGION = 1500

# Sidebar / footer carousels ("gerelateerd aanbod", "recent verhuurd/verkocht")
# hold OTHER listings' cards. Their status badges must never be read as the
# primary listing's status, so these blocks are removed before matching.
SIDEBAR_RE = re.compile(
    r"<(aside|footer)\b[^>]*>.*?</\1>",
    re.IGNORECASE | re.DOTALL,
)


def header_region(body: str, window: int = DEFAULT_HEADER_REGION) -> str:
    """Return the slice around the listing's <h1> where a badge is trusted."""
    m = re.search(r"<h1\b", body, re.IGNORECASE)
    start = m.start() if m else 0
    return body[start : start + window]


def reads_gone(
    html: str,
    *,
    page_status_re: re.Pattern[str] | None,
    badge_status_re: re.Pattern[str],
    window: int = DEFAULT_HEADER_REGION,
) -> bool:
    """Page-scoped gone/sold detection (see module docstring).

    ``page_status_re`` matches unambiguous "this listing" phrases anywhere in the
    body (after stripping sidebar/footer carousels). ``badge_status_re`` matches
    bare status badges but only inside the page's header region.
    """
    body = SIDEBAR_RE.sub(" ", html)
    if page_status_re is not None and page_status_re.search(body):
        return True
    return bool(badge_status_re.search(header_region(body, window)))


def is_gone(
    url: str,
    *,
    fetch: Callable[[str], bytes],
    page_status_re: re.Pattern[str] | None,
    badge_status_re: re.Pattern[str],
    window: int = DEFAULT_HEADER_REGION,
    gone_http_codes: frozenset[int] = DEFAULT_GONE_HTTP_CODES,
) -> bool:
    """Return True when the listing page is a 404/410 or reads as gone/sold.

    ``fetch(url)`` returns the raw page bytes and may raise
    ``urllib.error.HTTPError`` (mapped to a gone/not-gone decision via
    ``gone_http_codes``). Any other exception propagates so the caller can skip
    the listing without marking it gone.
    """
    try:
        body = fetch(url)
    except urllib.error.HTTPError as exc:
        return exc.code in gone_http_codes
    return reads_gone(
        body.decode("utf-8", errors="ignore"),
        page_status_re=page_status_re,
        badge_status_re=badge_status_re,
        window=window,
    )


def run_recheck(
    items: Iterable[Any],
    *,
    mark_checked: Callable[[Any], None],
    gone: Callable[[Any], bool],
    on_gone: Callable[[Any], str | None],
    on_error: Callable[[Any, Exception], None] | None = None,
) -> list[str]:
    """Round-robin recheck loop shared by both services.

    For every ``item`` the cursor is advanced first (``mark_checked``) so a
    persistently failing URL never blocks the queue; then ``gone(item)`` decides
    whether it transitioned. On a transition ``on_gone(item)`` performs the
    delete + status update and returns the address string (or None to skip it in
    the summary). Returns the list of removed addresses.
    """
    removed: list[str] = []
    for item in items:
        mark_checked(item)
        try:
            is_transitioned = gone(item)
        except Exception as exc:
            if on_error is not None:
                on_error(item, exc)
            continue
        if is_transitioned:
            addr = on_gone(item)
            if addr is not None:
                removed.append(addr)
    return removed


def build_summary_text(
    addresses: list[str],
    *,
    title_template: str,
    escape: Callable[[str], str],
) -> str:
    """Build the batched summary body.

    ``title_template`` is formatted with ``count`` and ``word`` (correctly
    pluralised) and should already contain the leading emoji and ``<b>…</b>``.
    """
    count = len(addresses)
    word = "woning" if count == 1 else "woningen"
    listing_lines = "\n".join(f"• {escape(a)}" for a in addresses)
    title = title_template.format(count=count, word=word)
    return f"{title}\n\n{listing_lines}"


def send_replaceable_summary(
    addresses: list[str],
    *,
    title_template: str,
    escape: Callable[[str], str],
    delete_previous: Callable[[], None],
    broadcast: Callable[[str], Any],
) -> Any:
    """Delete the previous summary, send a fresh one, return the new send result.

    The caller owns summary-id persistence (kv row vs in-memory), passing a
    ``delete_previous`` that removes the last summary and a ``broadcast`` that
    sends the new text and returns whatever id structure it stores.
    """
    delete_previous()
    text = build_summary_text(
        addresses, title_template=title_template, escape=escape
    )
    return broadcast(text)
