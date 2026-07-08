"""Tests for the Telegram issue-report intake (chat_log table + update loop).

Covers: schema creation, the 14-day purge, and the update-loop rule that
free-text group messages are logged while listing-URL submissions, commands and
bot messages are not.

responder.py imports detection (scrapling) and form_filler (camoufox) at module
level; those browser stacks aren't needed here, so we stub them before import
(mirroring test_add_site_routing.py).
"""

import sys
import threading
import types

import pytest

# Stub the heavy browser dependencies pulled in transitively by responder.py.
if "scrapling.fetchers" not in sys.modules:
    scrapling_pkg = sys.modules.setdefault("scrapling", types.ModuleType("scrapling"))
    fetchers_mod = types.ModuleType("scrapling.fetchers")
    fetchers_mod.StealthyFetcher = object
    scrapling_pkg.fetchers = fetchers_mod
    sys.modules["scrapling.fetchers"] = fetchers_mod
if "camoufox.sync_api" not in sys.modules:
    camoufox_pkg = sys.modules.setdefault("camoufox", types.ModuleType("camoufox"))
    sync_api_mod = types.ModuleType("camoufox.sync_api")
    sync_api_mod.Camoufox = object
    camoufox_pkg.sync_api = sync_api_mod
    sys.modules["camoufox.sync_api"] = sync_api_mod

import config  # noqa: E402
import db  # noqa: E402
import responder  # noqa: E402
import tg  # noqa: E402

RENTALS_CHAT = "-100"
SALES_CHAT = "-200"


@pytest.fixture
def rdb(tmp_path, monkeypatch):
    """Fresh temp responder DB with the rental/sales chats configured."""
    monkeypatch.setattr(db, "DB_PATH", str(tmp_path / "db.sqlite"))
    monkeypatch.setattr(db, "_local", threading.local())
    monkeypatch.setattr(config, "TELEGRAM_CHAT_IDS", [RENTALS_CHAT])
    monkeypatch.setattr(config, "TELEGRAM_SALES_CHAT_IDS", [SALES_CHAT])
    db.init_schema()
    return db.conn()


def _chat_log_rows(c):
    return c.execute(
        "SELECT chat_id, message_id, sender_name, sender_username, ts, text "
        "FROM chat_log ORDER BY id"
    ).fetchall()


def _message(text, *, chat=RENTALS_CHAT, **from_extra):
    sender = {"first_name": "Alice", "username": "alice", "id": 7}
    sender.update(from_extra)
    return {
        "chat": {"id": int(chat)},
        "message_id": 55,
        "date": 1_700_000_000,
        "from": sender,
        "text": text,
    }


# ---------------------------------------------------------------------------
# Schema
# ---------------------------------------------------------------------------


def test_init_schema_creates_chat_log(rdb):
    cols = {row["name"] for row in rdb.execute("PRAGMA table_info(chat_log)")}
    assert {"chat_id", "message_id", "sender_name", "sender_username", "ts", "text"} <= cols


# ---------------------------------------------------------------------------
# Update-loop logging rules
# ---------------------------------------------------------------------------


def test_free_text_message_is_logged(rdb):
    responder._handle_message(_message("de knop werkt niet meer"))
    rows = _chat_log_rows(rdb)
    assert len(rows) == 1
    assert rows[0]["chat_id"] == RENTALS_CHAT
    assert rows[0]["text"] == "de knop werkt niet meer"
    assert rows[0]["sender_name"] == "Alice"
    assert rows[0]["sender_username"] == "alice"
    assert rows[0]["message_id"] == 55


def test_free_text_from_sales_chat_is_logged(rdb):
    responder._handle_message(_message("kan de prijsfilter omhoog?", chat=SALES_CHAT))
    assert len(_chat_log_rows(rdb)) == 1


def test_listing_url_submission_is_not_logged(rdb, monkeypatch):
    # A URL is treated as an add-site submission (existing flow) — never logged.
    monkeypatch.setattr(responder, "_propose_add_site", lambda *a, **k: None)
    responder._handle_message(_message("check https://foo.nl/huis/1"))
    assert _chat_log_rows(rdb) == []


def test_bare_url_submission_routes_to_add_site_not_logged(rdb, monkeypatch):
    # A message that is *only* the URL is a bare add-site submission.
    calls = []
    monkeypatch.setattr(
        responder, "_propose_add_site", lambda chat_id, url, **k: calls.append(url)
    )
    responder._handle_message(_message("https://foo.nl/huis/1"))
    assert calls == ["https://foo.nl/huis/1"]
    assert _chat_log_rows(rdb) == []


def test_short_prefix_url_routes_to_add_site_not_logged(rdb, monkeypatch):
    # URL plus a short (<=15 char) prefix still counts as a submission.
    calls = []
    monkeypatch.setattr(
        responder, "_propose_add_site", lambda chat_id, url, **k: calls.append(url)
    )
    responder._handle_message(_message("check https://x.nl/woning/1"))
    assert calls == ["https://x.nl/woning/1"]
    assert _chat_log_rows(rdb) == []


def test_sentence_mentioning_url_is_logged_not_add_site(rdb, monkeypatch):
    # Prose that merely mentions a link (>15 surrounding chars) is an issue
    # report: it is logged and never opens an add-site request.
    calls = []
    monkeypatch.setattr(
        responder, "_propose_add_site", lambda *a, **k: calls.append(a)
    )
    report = "de knop bij https://foo.nl/huis/1 werkt niet meer"
    responder._handle_message(_message(report))
    assert calls == []
    rows = _chat_log_rows(rdb)
    assert len(rows) == 1
    assert rows[0]["text"] == report


def test_commands_are_not_logged(rdb, monkeypatch):
    monkeypatch.setattr(responder, "_send_status", lambda chat_id: None)
    monkeypatch.setattr(responder, "_send_help", lambda chat_id: None)
    responder._handle_message(_message("/status"))
    responder._handle_message(_message("/help"))
    assert _chat_log_rows(rdb) == []


def test_bot_own_message_is_not_logged(rdb):
    responder._handle_message(_message("ik ben een bot", is_bot=True))
    assert _chat_log_rows(rdb) == []


def test_unknown_chat_is_not_logged(rdb):
    responder._handle_message(_message("hoi", chat="-999"))
    assert _chat_log_rows(rdb) == []


def test_logged_message_stores_iso_timestamp(rdb):
    responder._handle_message(_message("iets"))
    ts = _chat_log_rows(rdb)[0]["ts"]
    # ISO-8601 with the unix `date` converted to UTC.
    assert ts.startswith("2023-11-14T")


# ---------------------------------------------------------------------------
# 14-day purge
# ---------------------------------------------------------------------------


def test_purge_removes_old_rows_keeps_recent(rdb):
    rdb.execute(
        "INSERT INTO chat_log (chat_id, message_id, sender_name, sender_username, ts, text) "
        "VALUES (?, ?, ?, ?, datetime('now', '-20 days'), ?)",
        (RENTALS_CHAT, 1, "Old", "old", "ancient report"),
    )
    rdb.execute(
        "INSERT INTO chat_log (chat_id, message_id, sender_name, sender_username, ts, text) "
        "VALUES (?, ?, ?, ?, datetime('now', '-2 days'), ?)",
        (RENTALS_CHAT, 2, "New", "new", "recent report"),
    )
    rdb.commit()

    removed = db.purge_old_chat_log()
    assert removed == 1
    rows = _chat_log_rows(rdb)
    assert len(rows) == 1
    assert rows[0]["text"] == "recent report"


def test_purge_respects_custom_days(rdb):
    rdb.execute(
        "INSERT INTO chat_log (chat_id, message_id, sender_name, sender_username, ts, text) "
        "VALUES (?, ?, ?, ?, datetime('now', '-5 days'), ?)",
        (RENTALS_CHAT, 1, "X", "x", "five days old"),
    )
    rdb.commit()
    assert db.purge_old_chat_log(days=3) == 1
