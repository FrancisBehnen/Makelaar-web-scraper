"""Tests for the rental delisting cleanup (mirrors sales-sidecar coverage).

Covers: schema auto-migration, the available-listings query, sold/gone status
detection (404/410 + conservative status regex incl. a false-positive guard),
message deletion + status transition, the batched summary, and graceful handling
of the Telegram 48h deleteMessage limit.

delisting.py only imports config/db/tg/letter — none pull in the browser stack —
so no scrapling/camoufox stubbing is needed here.
"""

import json
import re
import sqlite3
import threading
import urllib.error
import urllib.request

import pytest

import config
import db
import delisting
import tg


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def rdb(tmp_path, monkeypatch):
    """Fresh temp responder DB (responses + kv + a minimal houses table)."""
    monkeypatch.setattr(db, "DB_PATH", str(tmp_path / "db.sqlite"))
    monkeypatch.setattr(db, "_local", threading.local())
    monkeypatch.setattr(config, "RECHECK_BATCH_SIZE", 5)
    db.init_schema()
    c = db.conn()
    c.execute(
        """
        CREATE TABLE houses (
            url TEXT PRIMARY KEY,
            straatnaamHuisnummer TEXT,
            plaats TEXT,
            vraagprijs TEXT,
            oppervlakte TEXT,
            kamers TEXT
        )
        """
    )
    c.commit()
    return c


def _add_listing(
    c,
    url="https://a.nl/1",
    addr="Voorstraat 1",
    tg_ids='{"-100": 42}',
    listing_status="available",
    status="notified",
):
    c.execute(
        "INSERT INTO houses (url, straatnaamHuisnummer, plaats) VALUES (?, ?, ?)",
        (url, addr, "Delft"),
    )
    c.execute(
        "INSERT INTO responses (url, status, tg_message_ids, listing_status) "
        "VALUES (?, ?, ?, ?)",
        (url, status, tg_ids, listing_status),
    )
    c.commit()


def _no_delete(monkeypatch):
    calls = []
    monkeypatch.setattr(
        tg, "delete_message",
        lambda cid, mid: (calls.append((cid, mid)), True)[1],
    )
    return calls


# ---------------------------------------------------------------------------
# Schema migration
# ---------------------------------------------------------------------------


def test_init_schema_creates_new_columns(rdb):
    cols = {row["name"] for row in rdb.execute("PRAGMA table_info(responses)")}
    assert "listing_status" in cols
    assert "last_checked_at" in cols


def test_init_schema_migrates_existing_table(tmp_path, monkeypatch):
    db_path = str(tmp_path / "db.sqlite")
    monkeypatch.setattr(db, "DB_PATH", db_path)
    monkeypatch.setattr(db, "_local", threading.local())
    # Old-style responses table without the delisting columns.
    conn = sqlite3.connect(db_path)
    conn.execute(
        """
        CREATE TABLE responses (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            url TEXT NOT NULL UNIQUE,
            status TEXT NOT NULL,
            tg_message_ids TEXT
        )
        """
    )
    conn.execute(
        "INSERT INTO responses (url, status, tg_message_ids) VALUES (?, ?, ?)",
        ("https://a.nl/1", "notified", '{"-100": 42}'),
    )
    conn.commit()
    conn.close()

    db.init_schema()
    c = db.conn()
    cols = {row["name"] for row in c.execute("PRAGMA table_info(responses)")}
    assert "listing_status" in cols
    assert "last_checked_at" in cols
    row = c.execute(
        "SELECT listing_status, last_checked_at FROM responses"
    ).fetchone()
    assert row["listing_status"] == "available"
    assert row["last_checked_at"] is None


# ---------------------------------------------------------------------------
# available_listings query
# ---------------------------------------------------------------------------


def test_available_listings_returns_notified_available(rdb):
    _add_listing(rdb)
    rows = db.available_listings(5)
    assert [r["url"] for r in rows] == ["https://a.nl/1"]
    assert rows[0]["straatnaamHuisnummer"] == "Voorstraat 1"


def test_available_listings_excludes_gone(rdb):
    _add_listing(rdb, listing_status="gone")
    assert db.available_listings(5) == []


@pytest.mark.parametrize("tg_ids", ["", "{}", None])
def test_available_listings_excludes_unnotified(rdb, tg_ids):
    _add_listing(rdb, tg_ids=tg_ids)
    assert db.available_listings(5) == []


@pytest.mark.parametrize("status", ["seeded", "duplicate", "cancelled"])
def test_available_listings_excludes_non_notified_status(rdb, status):
    _add_listing(rdb, status=status)
    assert db.available_listings(5) == []


def test_available_listings_orders_never_checked_first(rdb):
    _add_listing(rdb, url="https://a.nl/checked", addr="Checked 1")
    _add_listing(rdb, url="https://a.nl/fresh", addr="Fresh 1")
    # Mark the first one as already checked; the never-checked one must lead.
    rid = rdb.execute(
        "SELECT id FROM responses WHERE url = ?", ("https://a.nl/checked",)
    ).fetchone()["id"]
    db.touch_listing_checked(rid)
    rows = db.available_listings(5)
    assert rows[0]["url"] == "https://a.nl/fresh"


def test_mark_listing_gone_sets_status(rdb):
    _add_listing(rdb)
    rid = rdb.execute("SELECT id FROM responses").fetchone()["id"]
    db.mark_listing_gone(rid)
    row = rdb.execute("SELECT listing_status FROM responses").fetchone()
    assert row["listing_status"] == "gone"


# ---------------------------------------------------------------------------
# is_gone detection
# ---------------------------------------------------------------------------


class _FakeResp:
    def __init__(self, body: bytes):
        self._body = body

    def read(self):
        return self._body

    def __enter__(self):
        return self

    def __exit__(self, *a):
        pass


def _mock_urlopen(monkeypatch, body: bytes):
    monkeypatch.setattr(delisting, "_fetch", lambda req: _FakeResp(body))


def _mock_http_error(monkeypatch, code: int):
    def raise_error(req):
        raise urllib.error.HTTPError(req.full_url, code, "err", {}, None)

    monkeypatch.setattr(delisting, "_fetch", raise_error)


@pytest.mark.parametrize(
    "body",
    [
        b"<html>Deze woning is verhuurd</html>",
        b"<html>Status: Verhuurd</html>",
        b"<html>Verhuurd onder voorbehoud</html>",
        b"<html>Deze woning is onder optie</html>",
        b"<html>Niet meer beschikbaar</html>",
    ],
)
def test_is_gone_matches_rented_status(monkeypatch, body):
    _mock_urlopen(monkeypatch, body)
    assert delisting.is_gone("https://a.nl/1") is True


def test_is_gone_false_for_live_listing(monkeypatch):
    _mock_urlopen(
        monkeypatch, b"<html>Te huur - 3 kamers, 80 m2, beschikbaar</html>"
    )
    assert delisting.is_gone("https://a.nl/1") is False


def test_is_gone_false_positive_guard_recently_rented_widget(monkeypatch):
    # THE failure mode: a still-live listing whose page carries a "recent
    # verhuurd" carousel in an <aside>, and one of those neighbouring cards
    # carries a real status BADGE ("verhuurd onder voorbehoud"). Matching the
    # badge anywhere in the body would wrongly delete this live listing.
    body = (
        b"<html><main><h1>Kerkstraat 12</h1>"
        b"Te huur, beschikbaar per direct, 3 kamers</main>"
        b"<aside>Recent verhuurd: Marktplein 4 - verhuurd onder voorbehoud, "
        b"Havenweg 8 - onder optie</aside></html>"
    )
    _mock_urlopen(monkeypatch, body)
    assert delisting.is_gone("https://a.nl/1") is False


def test_is_gone_true_genuine_gone_page_with_badge(monkeypatch):
    # Same shape, but now it is THIS listing that is gone: the badge sits in the
    # header region next to the <h1>. Must be detected as gone.
    body = (
        b"<html><main><h1>Kerkstraat 12</h1>"
        b"<span class='badge'>Verhuurd onder voorbehoud</span></main>"
        b"<aside>Vergelijkbaar aanbod: Marktplein 4, Havenweg 8</aside></html>"
    )
    _mock_urlopen(monkeypatch, body)
    assert delisting.is_gone("https://a.nl/1") is True


def test_is_gone_false_for_badge_outside_header_region(monkeypatch):
    # A bare badge far below the listing header (e.g. a related block that is not
    # an <aside>) is outside the trusted window and must not mark the page gone.
    body = (
        b"<html><main><h1>Kerkstraat 12</h1>Te huur, beschikbaar</main>"
        + b"<div>filler</div>" * 400
        + b"<section class='related'>Marktplein 4 - verhuurd onder voorbehoud"
        b"</section></html>"
    )
    _mock_urlopen(monkeypatch, body)
    assert delisting.is_gone("https://a.nl/1") is False


@pytest.mark.parametrize("code", [404, 410])
def test_is_gone_true_on_404_410(monkeypatch, code):
    _mock_http_error(monkeypatch, code)
    assert delisting.is_gone("https://a.nl/1") is True


def test_is_gone_false_on_server_error(monkeypatch):
    _mock_http_error(monkeypatch, 500)
    assert delisting.is_gone("https://a.nl/1") is False


def test_is_gone_sends_huurstunt_cookie(monkeypatch):
    monkeypatch.setattr(config, "HUURSTUNT_COOKIE", "sess=abc; t=1")
    captured = {}

    def fake_fetch(req):
        captured["cookie"] = req.headers.get("Cookie")
        return _FakeResp(b"ok")

    monkeypatch.setattr(delisting, "_fetch", fake_fetch)
    delisting.is_gone("https://www.huurstunt.nl/huren/in/delft/x")
    assert captured["cookie"] == "sess=abc; t=1"


def test_is_gone_no_cookie_for_other_sites(monkeypatch):
    monkeypatch.setattr(config, "HUURSTUNT_COOKIE", "sess=abc")
    captured = {}

    def fake_fetch(req):
        captured["cookie"] = req.headers.get("Cookie")
        return _FakeResp(b"ok")

    monkeypatch.setattr(delisting, "_fetch", fake_fetch)
    delisting.is_gone("https://www.example.nl/huis/1")
    assert captured["cookie"] is None


# ---------------------------------------------------------------------------
# Cross-host redirect cookie safety
# ---------------------------------------------------------------------------


def _redirect(newurl, code=302):
    handler = delisting._CookieSafeRedirectHandler()
    req = urllib.request.Request(
        "https://www.huurstunt.nl/huren/in/delft/x",
        headers={"Cookie": "sess=abc", "User-Agent": "x"},
    )
    return handler.redirect_request(req, None, code, "Found", {}, newurl)


def test_redirect_drops_cookie_cross_host():
    new = _redirect("https://tracker.example.com/collect")
    assert new is not None
    assert new.get_header("Cookie") is None


def test_redirect_keeps_cookie_same_host():
    new = _redirect("https://www.huurstunt.nl/huren/in/delft/y")
    assert new is not None
    assert new.get_header("Cookie") == "sess=abc"


# ---------------------------------------------------------------------------
# _delete_listing_messages + recheck_delisted
# ---------------------------------------------------------------------------


def test_recheck_deletes_and_marks_gone(rdb, monkeypatch):
    _add_listing(rdb)
    calls = _no_delete(monkeypatch)
    monkeypatch.setattr(delisting, "is_gone", lambda url: True)

    removed = delisting.recheck_delisted()
    assert removed == [("Voorstraat 1", "https://a.nl/1")]
    assert calls == [("-100", 42)]
    row = rdb.execute("SELECT listing_status FROM responses").fetchone()
    assert row["listing_status"] == "gone"


def test_recheck_keeps_available_listing(rdb, monkeypatch):
    _add_listing(rdb)
    _no_delete(monkeypatch)
    monkeypatch.setattr(delisting, "is_gone", lambda url: False)

    removed = delisting.recheck_delisted()
    assert removed == []
    row = rdb.execute(
        "SELECT listing_status, last_checked_at FROM responses"
    ).fetchone()
    assert row["listing_status"] == "available"
    # Still advanced the round-robin cursor.
    assert row["last_checked_at"] is not None


def test_recheck_skips_on_fetch_failure(rdb, monkeypatch):
    _add_listing(rdb)
    _no_delete(monkeypatch)

    def boom(url):
        raise Exception("network error")

    monkeypatch.setattr(delisting, "is_gone", boom)
    removed = delisting.recheck_delisted()
    assert removed == []
    row = rdb.execute("SELECT listing_status FROM responses").fetchone()
    assert row["listing_status"] == "available"


def test_recheck_marks_gone_even_when_delete_fails_48h(rdb, monkeypatch):
    # Telegram rejects deleteMessage after 48h -> delete_message returns False,
    # but the listing must still be marked gone so it stops being re-checked.
    _add_listing(rdb)
    monkeypatch.setattr(tg, "delete_message", lambda cid, mid: False)
    monkeypatch.setattr(delisting, "is_gone", lambda url: True)

    removed = delisting.recheck_delisted()
    assert removed == [("Voorstraat 1", "https://a.nl/1")]
    row = rdb.execute("SELECT listing_status FROM responses").fetchone()
    assert row["listing_status"] == "gone"


def test_recheck_respects_batch_size(rdb, monkeypatch):
    for i in range(4):
        _add_listing(rdb, url=f"https://a.nl/{i}", addr=f"Straat {i}")
    monkeypatch.setattr(config, "RECHECK_BATCH_SIZE", 2)
    _no_delete(monkeypatch)
    checked = []
    monkeypatch.setattr(
        delisting, "is_gone", lambda url: checked.append(url) or False
    )
    delisting.recheck_delisted()
    assert len(checked) == 2


# ---------------------------------------------------------------------------
# send_gone_summary
# ---------------------------------------------------------------------------


def test_send_gone_summary_sends_and_stores_ids(rdb, monkeypatch):
    sent = []
    monkeypatch.setattr(
        tg, "broadcast",
        lambda text, **kw: (sent.append(text), {"-100": 200})[1],
    )
    monkeypatch.setattr(tg, "delete_message", lambda *a: True)

    delisting.send_gone_summary(
        [("Voorstraat 1", "https://a.nl/1"), ("Achterstraat 9", "https://a.nl/9")]
    )

    assert len(sent) == 1
    assert '<a href="https://a.nl/1">Voorstraat 1</a>' in sent[0]
    assert '<a href="https://a.nl/9">Achterstraat 9</a>' in sent[0]
    assert "2 woningen" in sent[0]
    assert json.loads(db.kv_get("gone_summary_ids")) == {"-100": 200}


def test_send_gone_summary_singular(rdb, monkeypatch):
    sent = []
    monkeypatch.setattr(
        tg, "broadcast", lambda text, **kw: (sent.append(text), {})[1]
    )
    monkeypatch.setattr(tg, "delete_message", lambda *a: True)

    delisting.send_gone_summary([("Voorstraat 1", "https://a.nl/1")])
    assert "1 woning" in sent[0]
    assert "1 woningen" not in sent[0]


def test_send_gone_summary_append_only_keeps_previous(rdb, monkeypatch):
    # Default append-only mode: the previous summary is NOT deleted, and the new
    # one carries a date-time stamp so the history stays distinguishable.
    monkeypatch.setattr(config, "SUMMARY_APPEND_ONLY", True)
    db.kv_set("gone_summary_ids", json.dumps({"-100": 150}))
    deleted = []
    sent = []
    monkeypatch.setattr(
        tg, "delete_message",
        lambda cid, mid: (deleted.append((cid, mid)), True)[1],
    )
    monkeypatch.setattr(
        tg, "broadcast", lambda text, **kw: (sent.append(text), {"-100": 201})[1]
    )

    delisting.send_gone_summary([("Markt 3", "https://a.nl/3")])
    assert deleted == []
    assert re.search(r"\(\d{2}-\d{2} \d{2}:\d{2}\)", sent[0])
    assert json.loads(db.kv_get("gone_summary_ids")) == {"-100": 201}


def test_send_gone_summary_replace_mode_deletes_previous(rdb, monkeypatch):
    monkeypatch.setattr(config, "SUMMARY_APPEND_ONLY", False)
    db.kv_set("gone_summary_ids", json.dumps({"-100": 150}))
    deleted = []
    monkeypatch.setattr(
        tg, "delete_message",
        lambda cid, mid: (deleted.append((cid, mid)), True)[1],
    )
    monkeypatch.setattr(tg, "broadcast", lambda text, **kw: {"-100": 201})

    delisting.send_gone_summary([("Markt 3", "https://a.nl/3")])
    assert ("-100", 150) in deleted
    assert json.loads(db.kv_get("gone_summary_ids")) == {"-100": 201}
