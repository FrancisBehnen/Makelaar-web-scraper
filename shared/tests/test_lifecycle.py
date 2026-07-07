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
