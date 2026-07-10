"""Direct unit tests for the shared listing-lifecycle machinery.

The responder and sales suites exercise these functions through their thin
adapters; this file locks the extracted logic on its own.
"""

import re
import urllib.error

import pytest

from shared import lifecycle

PAGE_RE = re.compile(r"deze woning is verhuurd|status:\s*verhuurd", re.IGNORECASE)
BADGE_RE = re.compile(r"verhuurd onder voorbehoud|onder optie", re.IGNORECASE)


# ---------------------------------------------------------------------------
# reads_gone (page-scoped detection)
# ---------------------------------------------------------------------------


def test_reads_gone_trusts_page_phrase_anywhere():
    assert lifecycle.reads_gone(
        "<html><body>lots of text ... Deze woning is verhuurd</body></html>",
        page_status_re=PAGE_RE,
        badge_status_re=BADGE_RE,
    )


def test_reads_gone_badge_only_in_header_region():
    body = (
        "<html><main><h1>Kerkstraat 12</h1>"
        "<span>Verhuurd onder voorbehoud</span></main></html>"
    )
    assert lifecycle.reads_gone(
        body, page_status_re=PAGE_RE, badge_status_re=BADGE_RE
    )


def test_reads_gone_strips_sidebar_carousel():
    body = (
        "<html><main><h1>Kerkstraat 12</h1>Beschikbaar</main>"
        "<aside>Recent verhuurd: Marktplein 4 - verhuurd onder voorbehoud</aside>"
        "</html>"
    )
    assert not lifecycle.reads_gone(
        body, page_status_re=PAGE_RE, badge_status_re=BADGE_RE
    )


def test_reads_gone_badge_outside_header_region_ignored():
    body = (
        "<html><main><h1>Kerkstraat 12</h1>Beschikbaar</main>"
        + "<div>filler</div>" * 400
        + "<section>Marktplein 4 - onder optie</section></html>"
    )
    assert not lifecycle.reads_gone(
        body, page_status_re=PAGE_RE, badge_status_re=BADGE_RE
    )


def test_reads_gone_none_page_re_only_badges():
    assert lifecycle.reads_gone(
        "<h1>x</h1> onder optie", page_status_re=None, badge_status_re=BADGE_RE
    )


# ---------------------------------------------------------------------------
# script/style stripping (vbtverhuurmakelaars false-positive regression)
# ---------------------------------------------------------------------------

# vbt is a React app whose JS bundle embeds an i18n string table on EVERY page,
# including a `propertyNotFound` string that reads like a genuine gone phrase.
# The page-status regex must NOT match it inside a <script> block.
VBT_PAGE_RE = re.compile(r"niet meer beschikbaar", re.IGNORECASE)


def test_reads_gone_ignores_phrase_inside_script():
    # A LIVE vbt listing (HTTP 200) whose i18n table lives in the JS bundle, not
    # in visible body text. Must NOT read as gone.
    body = (
        "<html><head>"
        '<script>window.__i18n={inactivepage:"Helaas, deze pagina is niet '
        '(meer) beschikbaar",propertyNotFound:"Helaas, deze woning is niet '
        'meer beschikbaar",foo:"bar"}</script></head>'
        "<body><main><h1>Kerkstraat 12</h1>"
        "Te huur, 3 kamers, beschikbaar per direct</main></body></html>"
    )
    assert not lifecycle.reads_gone(
        body, page_status_re=VBT_PAGE_RE, badge_status_re=BADGE_RE
    )


def test_reads_gone_true_for_visible_gone_phrase():
    # Same phrase, but now in visible body text -> genuinely gone.
    body = (
        "<html><body><main><h1>Kerkstraat 12</h1>"
        "Helaas, deze woning is niet meer beschikbaar</main></body></html>"
    )
    assert lifecycle.reads_gone(
        body, page_status_re=VBT_PAGE_RE, badge_status_re=BADGE_RE
    )


def test_reads_gone_ignores_phrase_inside_style_and_noscript():
    style_body = (
        "<html><head><style>/* niet meer beschikbaar */</style></head>"
        "<body><main><h1>x</h1>beschikbaar</main></body></html>"
    )
    noscript_body = (
        "<html><body><main><h1>x</h1>beschikbaar"
        "<noscript>niet meer beschikbaar</noscript></main></body></html>"
    )
    assert not lifecycle.reads_gone(
        style_body, page_status_re=VBT_PAGE_RE, badge_status_re=BADGE_RE
    )
    assert not lifecycle.reads_gone(
        noscript_body, page_status_re=VBT_PAGE_RE, badge_status_re=BADGE_RE
    )


def test_reads_gone_ignores_badge_inside_script():
    # The header-region badge match must also run on script-free text: a bare
    # badge string embedded in an inline <script> right after the <h1> must not
    # trip detection.
    body = (
        "<html><body><main><h1>Kerkstraat 12</h1>"
        '<script>var labels={status:"verhuurd onder voorbehoud"}</script>'
        "Te huur, beschikbaar</main></body></html>"
    )
    assert not lifecycle.reads_gone(
        body, page_status_re=VBT_PAGE_RE, badge_status_re=BADGE_RE
    )


# ---------------------------------------------------------------------------
# Funda koop: sold banner rendered ABOVE the <h1> (badge-before-h1 regression)
# ---------------------------------------------------------------------------

# The koop vocabulary the sales-sidecar passes in.
KOOP_PAGE_RE = re.compile(
    r"deze woning is verkocht|status:\s*verkocht", re.IGNORECASE
)
KOOP_BADGE_RE = re.compile(r"verkocht|onder bod|onder voorbehoud", re.IGNORECASE)

# Trimmed from the real live markup of the Achterom 1-B detail page: Funda emits
# its "Verkocht onder voorbehoud" badge as the first child of the `#about` block,
# ~290 chars BEFORE the address <h1> — a forward-only header window never sees it.
FUNDA_SOLD_HEADER = (
    '<div class="mx-auto -mr-4 -ml-4 h-px bg-neutral-20"></div>'
    '<div class="relative m-auto mt-6 flex flex-col gap-0 lg:grid">'
    '<div><div class="border-b border-solid border-neutral-20 pb-4" id="about">'
    '<div class="flex items-baseline mb-2 lg:mb-4">'
    '<div class="bg-red-70 inline-block rounded-xs px-2 text-xs font-bold '
    'text-white">Verkocht onder voorbehoud</div></div>'
    '<div class="grid grid-cols-[1fr_auto] gap-2"><div>'
    '<div class="relative flex justify-between" city="Delft" postcode="2611PL">'
    '<h1 class="md:pr-[4.2rem]" data-global-id="8050233">'
    '<span class="block text-2xl font-bold">Achterom 1-B</span>'
    '<span class="text-neutral-40">2611 PL Delft</span></h1></div></div>'
)


def test_reads_gone_funda_sold_banner_above_h1():
    # THE bug: the sold badge sits just above the address <h1>. Detection must
    # find it via the backward lookback in the header region.
    assert lifecycle.reads_gone(
        f"<html><body>{FUNDA_SOLD_HEADER}</body></html>",
        page_status_re=KOOP_PAGE_RE,
        badge_status_re=KOOP_BADGE_RE,
    )


def test_reads_gone_funda_live_recent_sold_carousel_not_gone():
    # A LIVE Funda koop listing: the address <h1> carries NO status badge above
    # or below it, and the only "verkocht" on the page lives in a "recent
    # verkocht" carousel (an <aside>) AND in the JS bundle's i18n string table
    # (a <script>). Neither may mark this live listing as sold.
    body = (
        "<html><head>"
        '<script>window.__i18n={soldStatus:"Verkocht onder voorbehoud",'
        'foo:"bar"}</script></head><body>'
        '<div class="border-b" id="about"><div class="flex items-baseline">'
        '<div class="grid"><div>'
        '<div class="relative flex justify-between" city="Delft">'
        '<h1 data-global-id="1"><span>Nieuwstraat 5</span>'
        "<span>2611 AA Delft</span></h1></div></div>"
        "<div>Vraagprijs 250.000 k.k. — 3 kamers — beschikbaar</div></div>"
        '<aside><h2>Recent verkocht in de buurt</h2>'
        "<div>Marktplein 4 — Verkocht onder voorbehoud</div>"
        "<div>Havenweg 8 — Verkocht</div></aside>"
        "</body></html>"
    )
    assert not lifecycle.reads_gone(
        body, page_status_re=KOOP_PAGE_RE, badge_status_re=KOOP_BADGE_RE
    )


def test_reads_gone_lookback_bounded_not_whole_page():
    # The backward lookback must stay small: a "verkocht" badge far ABOVE the
    # <h1> (beyond the lookback window, and not inside a stripped sidebar) must
    # NOT be read as this listing's status.
    body = (
        "<html><body><section>Uitgelicht: Parklaan 9 — Verkocht</section>"
        + "<div>filler</div>" * 200
        + "<main><h1>Nieuwstraat 5</h1>Vraagprijs 250.000, beschikbaar"
        "</main></body></html>"
    )
    assert not lifecycle.reads_gone(
        body, page_status_re=KOOP_PAGE_RE, badge_status_re=KOOP_BADGE_RE
    )


# ---------------------------------------------------------------------------
# is_gone (fetch + HTTP-code mapping)
# ---------------------------------------------------------------------------


def _fetch_ok(body: bytes):
    return lambda url: body


def test_is_gone_reads_body():
    assert lifecycle.is_gone(
        "http://x",
        fetch=_fetch_ok(b"Deze woning is verhuurd"),
        page_status_re=PAGE_RE,
        badge_status_re=BADGE_RE,
    )


def test_is_gone_live_body_false():
    assert not lifecycle.is_gone(
        "http://x",
        fetch=_fetch_ok(b"Te huur, beschikbaar"),
        page_status_re=PAGE_RE,
        badge_status_re=BADGE_RE,
    )


@pytest.mark.parametrize("code,expected", [(404, True), (410, True), (500, False)])
def test_is_gone_http_error_mapping(code, expected):
    def fetch(url):
        raise urllib.error.HTTPError(url, code, "err", {}, None)

    assert (
        lifecycle.is_gone(
            "http://x",
            fetch=fetch,
            page_status_re=PAGE_RE,
            badge_status_re=BADGE_RE,
        )
        is expected
    )


def test_is_gone_other_exception_propagates():
    def fetch(url):
        raise ConnectionError("boom")

    with pytest.raises(ConnectionError):
        lifecycle.is_gone(
            "http://x",
            fetch=fetch,
            page_status_re=PAGE_RE,
            badge_status_re=BADGE_RE,
        )


# ---------------------------------------------------------------------------
# run_recheck (round-robin loop)
# ---------------------------------------------------------------------------


def test_run_recheck_touches_before_check_and_collects():
    order = []
    removed = lifecycle.run_recheck(
        ["a", "b", "c"],
        mark_checked=lambda x: order.append(("touch", x)),
        gone=lambda x: (order.append(("check", x)), x != "b")[1],
        on_gone=lambda x: f"addr-{x}",
    )
    assert removed == ["addr-a", "addr-c"]
    # Every item is touched *before* it is checked.
    assert order[0] == ("touch", "a")
    assert order[1] == ("check", "a")


def test_run_recheck_skips_on_error_but_still_touches():
    touched = []

    def gone(x):
        raise RuntimeError("fetch failed")

    errors = []
    removed = lifecycle.run_recheck(
        ["a"],
        mark_checked=touched.append,
        gone=gone,
        on_gone=lambda x: "addr",
        on_error=lambda item, exc: errors.append((item, str(exc))),
    )
    assert removed == []
    assert touched == ["a"]
    assert errors == [("a", "fetch failed")]


def test_run_recheck_on_gone_none_skipped():
    removed = lifecycle.run_recheck(
        ["a"],
        mark_checked=lambda x: None,
        gone=lambda x: True,
        on_gone=lambda x: None,
    )
    assert removed == []


# ---------------------------------------------------------------------------
# summary
# ---------------------------------------------------------------------------


def test_build_summary_text_pluralises():
    single = lifecycle.build_summary_text(
        [("A", "http://a")], title_template="{count} {word} weg", escape=lambda s: s
    )
    plural = lifecycle.build_summary_text(
        [("A", "http://a"), ("B", "http://b")],
        title_template="{count} {word} weg",
        escape=lambda s: s,
    )
    assert single.startswith("1 woning weg")
    assert plural.startswith("2 woningen weg")
    assert '• <a href="http://a">A</a>' in plural
    assert '• <a href="http://b">B</a>' in plural


def test_build_summary_text_links_address_to_url():
    text = lifecycle.build_summary_text(
        [("Voorstraat 1", "https://x.nl/huis/1")],
        title_template="{count} {word}",
        escape=lambda s: s,
    )
    assert '• <a href="https://x.nl/huis/1">Voorstraat 1</a>' in text


def test_build_summary_text_escapes_address_and_url():
    text = lifecycle.build_summary_text(
        [("A & B", "http://x?a=1&b=2")],
        title_template="{count} {word}",
        escape=lambda s: s.replace("&", "&amp;"),
    )
    assert '<a href="http://x?a=1&amp;b=2">A &amp; B</a>' in text


def test_build_summary_text_appends_timestamp():
    text = lifecycle.build_summary_text(
        [("A", "http://a")],
        title_template="🗑 <b>{count} {word} weg</b>",
        escape=lambda s: s,
        timestamp="08-07 14:32",
    )
    assert text.startswith("🗑 <b>1 woning weg</b> (08-07 14:32)")


def test_send_replaceable_summary_deletes_then_sends():
    events = []
    result = lifecycle.send_replaceable_summary(
        [("A", "http://a")],
        title_template="{count} {word}",
        escape=lambda s: s,
        delete_previous=lambda: events.append("delete"),
        broadcast=lambda text: (events.append(("send", text)), {"id": 1})[1],
    )
    assert result == {"id": 1}
    assert events[0] == "delete"
    assert events[1][0] == "send"
    assert '<a href="http://a">A</a>' in events[1][1]


def test_send_replaceable_summary_append_only_skips_delete():
    events = []
    result = lifecycle.send_replaceable_summary(
        [("A", "http://a")],
        title_template="{count} {word}",
        escape=lambda s: s,
        delete_previous=lambda: events.append("delete"),
        broadcast=lambda text: (events.append(("send", text)), {"id": 2})[1],
        append_only=True,
        timestamp="08-07 14:32",
    )
    assert result == {"id": 2}
    # No delete in append-only mode; only the send happened.
    assert "delete" not in events
    assert events[0][0] == "send"
    assert "(08-07 14:32)" in events[0][1]


def test_send_replaceable_summary_append_only_default_timestamp():
    sent = []
    lifecycle.send_replaceable_summary(
        [("A", "http://a")],
        title_template="{count} {word}",
        escape=lambda s: s,
        delete_previous=lambda: None,
        broadcast=lambda text: sent.append(text),
        append_only=True,
    )
    # A date-time stamp (dd-mm HH:MM) is auto-generated when none is supplied.
    assert re.search(r"\(\d{2}-\d{2} \d{2}:\d{2}\)", sent[0])


# ---------------------------------------------------------------------------
# dedup_by_url
# ---------------------------------------------------------------------------


def test_dedup_by_url_preserves_first_seen_order():
    out = lifecycle.dedup_by_url(
        [("A", "http://a"), ("B", "http://b"), ("A2", "http://a")]
    )
    assert out == [("A", "http://a"), ("B", "http://b")]


# ---------------------------------------------------------------------------
# upsert_accumulating_summary
# ---------------------------------------------------------------------------

from datetime import datetime, timedelta, timezone  # noqa: E402


def _harness(now_value):
    """A tiny in-memory harness capturing the injected IO of the summary."""

    box = {"state": None}
    events = []

    def load_state():
        return box["state"]

    def save_state(state):
        box["state"] = state

    def edit(message_ids, text):
        events.append(("edit", list(message_ids), text))
        return True

    def send(text):
        events.append(("send", text))
        return [{"chat_id": "-100", "message_id": 100 + len(events)}]

    def delete(message_ids):
        events.append(("delete", list(message_ids)))

    build_text = lambda entries: lifecycle.build_summary_text(  # noqa: E731
        entries, title_template="🗑 {count} {word}", escape=lambda s: s
    )

    def run(new_entries, *, now=now_value, **kw):
        return lifecycle.upsert_accumulating_summary(
            new_entries,
            load_state=load_state,
            save_state=save_state,
            edit=edit,
            send=send,
            delete=delete,
            build_text=build_text,
            now=lambda: now,
            **kw,
        )

    return box, events, run


_T0 = datetime(2026, 7, 10, 9, 0, tzinfo=timezone.utc)


def test_upsert_first_call_sends_then_edits():
    box, events, run = _harness(_T0)
    run([("A", "http://a")])
    assert [e[0] for e in events] == ["send"]  # first message is a send
    run([("B", "http://b")])
    # Second call edits the SAME message rather than sending (no push).
    assert [e[0] for e in events] == ["send", "edit"]
    # And the edit body accumulates both entries.
    _, _, text = events[1]
    assert '<a href="http://a">A</a>' in text
    assert '<a href="http://b">B</a>' in text
    assert "2 woningen" in text


def test_upsert_accumulates_and_dedups_by_url():
    box, events, run = _harness(_T0)
    run([("A", "http://a")])
    run([("A again", "http://a"), ("B", "http://b")])
    assert box["state"]["entries"] == [["A", "http://a"], ["B", "http://b"]]


def test_upsert_daily_roll_starts_new_message():
    box, events, run = _harness(_T0)
    run([("A", "http://a")])
    next_day = datetime(2026, 7, 11, 9, 0, tzinfo=timezone.utc)
    run([("B", "http://b")], now=next_day)
    # A new day => a fresh send (history preserved), NOT an edit.
    assert [e[0] for e in events] == ["send", "send"]
    # New message resets to only the new day's entry.
    assert box["state"]["entries"] == [["B", "http://b"]]
    assert box["state"]["day"] == "2026-07-11"


def test_upsert_entry_cap_rolls_new_message():
    box, events, run = _harness(_T0)
    run([("A", "http://a")])
    run([("B", "http://b"), ("C", "http://c")], max_entries=2)
    # Merged would be 3 > cap 2 => roll to a fresh message.
    assert [e[0] for e in events] == ["send", "send"]
    assert box["state"]["entries"] == [["B", "http://b"], ["C", "http://c"]]


def test_upsert_char_cap_rolls_new_message():
    box, events, run = _harness(_T0)
    run([("A", "http://a")])
    run([("B", "http://b")], max_chars=10)  # any real body exceeds 10 chars
    assert [e[0] for e in events] == ["send", "send"]


def test_upsert_roll_after_hours():
    # Same local day, but the current summary is older than roll_after_hours ->
    # roll to a fresh message (isolates the age threshold from the day change).
    box, events, run = _harness(_T0)
    run([("A", "http://a")], roll_after_hours=2)
    later = _T0 + timedelta(hours=3)
    run([("B", "http://b")], now=later, roll_after_hours=2)
    assert [e[0] for e in events] == ["send", "send"]


def test_upsert_edit_failure_deletes_then_resends():
    box = {"state": None}
    events = []
    box["state"] = {
        "message_ids": [{"chat_id": "-100", "message_id": 5}],
        "entries": [["A", "http://a"]],
        "created_at": _T0.isoformat(),
        "day": "2026-07-10",
    }
    build_text = lambda entries: "body"  # noqa: E731
    result = lifecycle.upsert_accumulating_summary(
        [("B", "http://b")],
        load_state=lambda: box["state"],
        save_state=lambda st: box.__setitem__("state", st),
        edit=lambda ids, text: events.append(("edit", ids)) or False,
        send=lambda text: (events.append(("send", text)), [{"chat_id": "-100", "message_id": 9}])[1],
        delete=lambda ids: events.append(("delete", ids)),
        build_text=build_text,
        now=lambda: _T0,
    )
    assert [e[0] for e in events] == ["edit", "delete", "send"]
    assert result["message_ids"] == [{"chat_id": "-100", "message_id": 9}]


def test_upsert_state_round_trips_across_restart():
    # First "process": build up some state.
    box, events, run = _harness(_T0)
    run([("A", "http://a")])
    persisted = box["state"]
    # Simulate a restart: a brand new harness that only sees persisted state.
    box2, events2, run2 = _harness(_T0)
    box2["state"] = persisted
    run2([("B", "http://b")])
    # It edited the surviving message rather than sending a new one.
    assert [e[0] for e in events2] == ["edit"]
    assert box2["state"]["entries"] == [["A", "http://a"], ["B", "http://b"]]
