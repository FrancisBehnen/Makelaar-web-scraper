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
* :func:`send_replaceable_summary` — build + send one summary. In *replace*
  mode it deletes the previous summary first; in *append-only* mode it leaves
  the previous summary in place (a visible history) and stamps each entry with
  a date-time so the history stays distinguishable.
"""

import re
import urllib.error
from collections.abc import Callable, Iterable
from datetime import datetime, timedelta, timezone
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

# Format for the date-time stamp appended to append-only summary titles.
SUMMARY_TIMESTAMP_FORMAT = "%d-%m %H:%M"

# Summaries stamp local Dutch wall-clock time, not the container's UTC clock.
_SUMMARY_TZ_NAME = "Europe/Amsterdam"


def _summary_now() -> datetime:
    """Current time in Europe/Amsterdam, falling back to UTC if the tz database
    is unavailable (e.g. a slim image without the ``tzdata`` package). The full
    ``python:3.12-bookworm`` images this ships in include system tzdata, so the
    fallback only guards degraded/test environments."""
    try:
        return datetime.now(ZoneInfo(_SUMMARY_TZ_NAME))
    except ZoneInfoNotFoundError:
        return datetime.now(timezone.utc)

# HTTP statuses that mean the listing page is gone for good.
DEFAULT_GONE_HTTP_CODES = frozenset({404, 410})

# Window (chars) read *forward* from the listing's <h1> in which a bare status
# badge counts.
DEFAULT_HEADER_REGION = 1500

# Window (chars) read *backward* from the listing's <h1>. Some sites render the
# status banner just ABOVE the address heading rather than below it. Funda koop,
# for example, puts its "Verkocht onder voorbehoud" badge as the first child of
# the detail's `#about` block, ~300 chars before the <h1> — a forward-only window
# never sees it. This lookback is kept small (and, like the forward window, runs
# on the script/style- and sidebar/footer-stripped body) so it stays inside the
# page's own header area and can't reach a "recent verkocht/verhuurd" carousel.
DEFAULT_HEADER_LOOKBACK = 500

# Sidebar / footer carousels ("gerelateerd aanbod", "recent verhuurd/verkocht")
# hold OTHER listings' cards. Their status badges must never be read as the
# primary listing's status, so these blocks are removed before matching.
SIDEBAR_RE = re.compile(
    r"<(aside|footer)\b[^>]*>.*?</\1>",
    re.IGNORECASE | re.DOTALL,
)

# Non-content blocks whose text is never visible listing status. React/SPA
# bundles routinely embed an i18n string table (with phrases like
# "deze woning is niet meer beschikbaar") inside <script> on EVERY page,
# including live listings — matching those would mark every page gone. These
# blocks are stripped before any phrase/badge matching. The trailing
# ``(?:</\1>|\Z)`` alternative tolerates an unclosed tag (strip to end of body).
SCRIPT_RE = re.compile(
    r"<(script|style|noscript|template)\b[^>]*>.*?(?:</\1>|\Z)",
    re.IGNORECASE | re.DOTALL,
)


def header_region(
    body: str,
    window: int = DEFAULT_HEADER_REGION,
    lookback: int = DEFAULT_HEADER_LOOKBACK,
) -> str:
    """Return the slice around the listing's <h1> where a badge is trusted.

    The slice spans ``lookback`` chars *before* the <h1> through ``window`` chars
    *after* it, so a badge rendered just above the address heading (e.g. Funda's
    "Verkocht onder voorbehoud" banner) is covered as well as one below it. When
    there is no <h1> the region is anchored at the body start (lookback is a
    no-op there).
    """
    m = re.search(r"<h1\b", body, re.IGNORECASE)
    start = m.start() if m else 0
    return body[max(0, start - lookback) : start + window]


def reads_gone(
    html: str,
    *,
    page_status_re: re.Pattern[str] | None,
    badge_status_re: re.Pattern[str],
    window: int = DEFAULT_HEADER_REGION,
    lookback: int = DEFAULT_HEADER_LOOKBACK,
) -> bool:
    """Page-scoped gone/sold detection (see module docstring).

    ``page_status_re`` matches unambiguous "this listing" phrases anywhere in the
    body (after stripping <script>/<style>/<noscript>/<template> blocks — which
    on SPA pages embed an i18n string table — and sidebar/footer carousels).
    ``badge_status_re`` matches bare status badges but only inside the page's
    header region (also computed from the script-free body) — the span from
    ``lookback`` chars before the <h1> through ``window`` chars after it.
    """
    body = SCRIPT_RE.sub(" ", html)
    body = SIDEBAR_RE.sub(" ", body)
    if page_status_re is not None and page_status_re.search(body):
        return True
    return bool(badge_status_re.search(header_region(body, window, lookback)))


def is_gone(
    url: str,
    *,
    fetch: Callable[[str], bytes],
    page_status_re: re.Pattern[str] | None,
    badge_status_re: re.Pattern[str],
    window: int = DEFAULT_HEADER_REGION,
    lookback: int = DEFAULT_HEADER_LOOKBACK,
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
        lookback=lookback,
    )


def run_recheck(
    items: Iterable[Any],
    *,
    mark_checked: Callable[[Any], None],
    gone: Callable[[Any], bool],
    on_gone: Callable[[Any], Any | None],
    on_error: Callable[[Any, Exception], None] | None = None,
) -> list[Any]:
    """Round-robin recheck loop shared by both services.

    For every ``item`` the cursor is advanced first (``mark_checked``) so a
    persistently failing URL never blocks the queue; then ``gone(item)`` decides
    whether it transitioned. On a transition ``on_gone(item)`` performs the
    delete + status update and returns a summary entry — an ``(address, url)``
    pair (or None to skip it in the summary). Returns the list of removed
    entries.
    """
    removed: list[Any] = []
    for item in items:
        mark_checked(item)
        try:
            is_transitioned = gone(item)
        except Exception as exc:
            if on_error is not None:
                on_error(item, exc)
            continue
        if is_transitioned:
            entry = on_gone(item)
            if entry is not None:
                removed.append(entry)
    return removed


def build_summary_text(
    listings: list[tuple[str, str]],
    *,
    title_template: str,
    escape: Callable[[str], str],
    timestamp: str | None = None,
) -> str:
    """Build the batched summary body.

    ``listings`` is a list of ``(address, url)`` pairs; each bullet links the
    (HTML-escaped) address to its listing URL. The messages are sent with
    ``parse_mode=HTML`` and link previews disabled, so a bullet is
    ``• <a href="URL">address</a>``.

    ``title_template`` is formatted with ``count`` and ``word`` (correctly
    pluralised) and should already contain the leading emoji and ``<b>…</b>``.
    When ``timestamp`` is given it is appended to the title line (outside the
    bold) so successive entries in an append-only history stay distinguishable.
    """
    count = len(listings)
    word = "woning" if count == 1 else "woningen"
    listing_lines = "\n".join(
        f'• <a href="{escape(url)}">{escape(address)}</a>'
        for address, url in listings
    )
    title = title_template.format(count=count, word=word)
    if timestamp:
        title = f"{title} ({timestamp})"
    return f"{title}\n\n{listing_lines}"


def send_replaceable_summary(
    listings: list[tuple[str, str]],
    *,
    title_template: str,
    escape: Callable[[str], str],
    delete_previous: Callable[[], None],
    broadcast: Callable[[str], Any],
    append_only: bool = False,
    timestamp: str | None = None,
) -> Any:
    """Send one batched summary and return the new send result.

    In *replace* mode (``append_only=False``) the previous summary is deleted
    first via ``delete_previous`` so the chat only shows the latest batch. In
    *append-only* mode the delete is skipped — every batch leaves its own
    message behind (a visible history) and the title is stamped with a
    date-time so entries stay distinguishable. Flipping between the two is a
    one-line change at the call site; the previous-id persistence plumbing is
    kept intact in both modes.

    ``listings`` is a list of ``(address, url)`` pairs (see
    :func:`build_summary_text`). The caller owns summary-id persistence (kv row
    vs in-memory), passing a ``delete_previous`` that removes the last summary
    and a ``broadcast`` that sends the new text and returns whatever id
    structure it stores. ``timestamp`` may be supplied explicitly (tests); in
    append-only mode it defaults to the current local time.
    """
    if not append_only:
        delete_previous()
    if append_only and timestamp is None:
        timestamp = _summary_now().strftime(SUMMARY_TIMESTAMP_FORMAT)
    text = build_summary_text(
        listings,
        title_template=title_template,
        escape=escape,
        timestamp=timestamp,
    )
    return broadcast(text)


def dedup_by_url(entries: Iterable[tuple[str, str]]) -> list[tuple[str, str]]:
    """Deduplicate ``(address, url)`` pairs by URL, preserving first-seen order.

    Used to merge a cycle's freshly-removed listings into the accumulating
    summary without ever listing the same URL twice (the same listing can be
    re-detected as gone across cycles, or arrive from two code paths).
    """
    seen: set[str] = set()
    out: list[tuple[str, str]] = []
    for address, url in entries:
        if url in seen:
            continue
        seen.add(url)
        out.append((address, url))
    return out


def upsert_accumulating_summary(
    new_entries: Iterable[tuple[str, str]],
    *,
    load_state: Callable[[], dict | None],
    save_state: Callable[[dict], None],
    edit: Callable[[list[dict], str], bool],
    send: Callable[[str], list[dict]],
    build_text: Callable[[list[tuple[str, str]]], str],
    delete: Callable[[list[dict]], Any] | None = None,
    now: Callable[[], datetime] = _summary_now,
    max_entries: int = 40,
    max_chars: int = 3800,
    roll_after_hours: int = 47,
) -> dict:
    """Maintain ONE persistent, in-place-edited summary that accumulates entries.

    The old ``send_replaceable_summary`` sent (or replaced) a whole message every
    cycle, which pushes a Telegram notification each time. That is wrong for a
    *summary* — pushes should be reserved for genuinely new listings. This
    function instead keeps a single message per local day and **edits it in
    place** (``editMessageText`` is silent) as more listings go gone/sold, so the
    summary quietly grows without ever pinging the group again.

    State (a JSON-serialisable dict the caller persists) has the shape::

        {"message_ids": [{"chat_id": .., "message_id": ..}, ...],
         "entries": [[address, url], ...],
         "created_at": ISO-8601, "day": "YYYY-MM-DD"}

    All IO is injected so the logic is unit-testable and backend-agnostic:

    * ``load_state()`` / ``save_state(state)`` — persist the blob (kv row).
    * ``edit(message_ids, text) -> bool`` — edit the live message(s); returns
      False when it can't (message deleted, or past Telegram's edit window).
    * ``send(text) -> list[dict]`` — send a NEW message and return its ids. The
      caller MUST make this send silent (``disable_notification=True``) so even
      the day's first summary doesn't push.
    * ``delete(message_ids)`` — optional; used only to clear a stale message that
      can no longer be edited before sending its replacement.
    * ``build_text(entries) -> str`` — render the body (reuse
      :func:`build_summary_text`).
    * ``now()`` — current local time (defaults to Europe/Amsterdam).

    A **fresh** message is started (the old one left in place as history) when
    the local day changes, the current summary is older than ``roll_after_hours``
    (bounds message age/size and stays inside Telegram's edit window), or the
    accumulated list would exceed ``max_entries`` / ``max_chars``. On a roll the
    entry list resets to just ``new_entries``.
    """
    state = load_state() or {}
    prev_entries = [tuple(e) for e in state.get("entries", [])]
    message_ids: list[dict] = state.get("message_ids") or []
    created_at_raw = state.get("created_at")
    state_day = state.get("day")

    now_dt = now()
    today = now_dt.date().isoformat()
    merged = dedup_by_url([*prev_entries, *new_entries])

    # With no live message there is nothing to edit — always start fresh.
    roll = not message_ids
    if message_ids:
        if state_day != today:
            roll = True
        elif created_at_raw:
            created_at = datetime.fromisoformat(created_at_raw)
            if now_dt - created_at > timedelta(hours=roll_after_hours):
                roll = True
        # Cap growth: a runaway summary would blow past Telegram's 4096-char
        # message limit and become unreadable.
        if len(merged) > max_entries or len(build_text(merged)) > max_chars:
            roll = True

    if roll:
        entries = dedup_by_url(list(new_entries))
        new_ids = send(build_text(entries))
        new_state = {
            "message_ids": new_ids,
            "entries": [list(e) for e in entries],
            "created_at": now_dt.isoformat(),
            "day": today,
        }
        save_state(new_state)
        return new_state

    text = build_text(merged)
    if not edit(message_ids, text):
        # The current summary can't be edited (deleted, or older than Telegram's
        # edit window). Clear the stale message if possible, then send a
        # replacement so the chat isn't left with a frozen, out-of-date summary.
        if delete is not None:
            delete(message_ids)
        message_ids = send(text)
    new_state = {
        "message_ids": message_ids,
        "entries": [list(e) for e in merged],
        "created_at": created_at_raw or now_dt.isoformat(),
        "day": state_day or today,
    }
    save_state(new_state)
    return new_state
