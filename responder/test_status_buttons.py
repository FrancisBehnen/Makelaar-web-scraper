"""Tests for the per-listing status buttons (reacted / invited / rejected /
dismiss) and their stateless callback dispatch.

responder.py imports detection (scrapling) and form_filler (camoufox) at module
level; those browser stacks aren't needed here, so stub them before import
(mirrors test_add_site_routing.py).
"""

import sys
import threading
import types

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

import pytest  # noqa: E402

import config  # noqa: E402
import db  # noqa: E402
import responder  # noqa: E402
import tg  # noqa: E402

RENTALS_CHAT = "-100"
SALES_CHAT = "-200"


# ---------------------------------------------------------------------------
# Keyboard construction
# ---------------------------------------------------------------------------


def test_status_button_row_shape():
    row = tg.status_button_row()
    assert [b["callback_data"] for b in row] == ["st:r", "st:i", "st:x", "st:d"]
    assert [b["text"] for b in row] == ["✅", "📅", "❌", "🗑"]
    # Callback data well under Telegram's 64-byte limit.
    assert all(len(b["callback_data"].encode()) <= 64 for b in row)


def test_status_button_row_is_fresh_each_call():
    a = tg.status_button_row()
    a[0]["text"] = "mutated"
    assert tg.status_button_row()[0]["text"] == "✅"


def test_notification_keyboard_email_has_brief_and_status_row():
    kb = responder._notification_keyboard(7, "email")
    rows = kb["inline_keyboard"]
    assert len(rows) == 2
    assert rows[0] == [{"text": "📋 Brief", "callback_data": "brief:7"}]
    assert [b["callback_data"] for b in rows[1]] == ["st:r", "st:i", "st:x", "st:d"]


def test_notification_keyboard_form_keeps_fill_and_adds_status_row():
    kb = responder._notification_keyboard(7, "form")
    rows = kb["inline_keyboard"]
    assert len(rows) == 2
    assert [b["callback_data"] for b in rows[0]] == ["brief:7", "fill:7"]
    assert [b["callback_data"] for b in rows[1]] == ["st:r", "st:i", "st:x", "st:d"]


def test_status_row_literal_shape_shared_with_sales():
    # Both services must emit this exact JSON shape for the stateless handler;
    # the sales-sidecar mirrors it in test_status_buttons_shape there.
    assert tg.status_button_row() == [
        {"text": "✅", "callback_data": "st:r"},
        {"text": "📅", "callback_data": "st:i"},
        {"text": "❌", "callback_data": "st:x"},
        {"text": "🗑", "callback_data": "st:d"},
    ]


# ---------------------------------------------------------------------------
# Callback dispatch — reactions
# ---------------------------------------------------------------------------


class _Recorder:
    def __init__(self, monkeypatch, *, reaction_ok=True):
        self.answers: list[str | None] = []
        self.reactions: list[tuple] = []
        self.edits: list[dict] = []
        self.deletes: list[tuple] = []
        monkeypatch.setattr(config, "TELEGRAM_CHAT_IDS", [RENTALS_CHAT])
        monkeypatch.setattr(config, "TELEGRAM_SALES_CHAT_IDS", [SALES_CHAT])
        monkeypatch.setattr(
            tg, "answer_callback",
            lambda cid, text=None: self.answers.append(text),
        )
        monkeypatch.setattr(
            tg, "set_reaction",
            lambda cid, mid, emoji: (
                self.reactions.append((cid, mid, emoji)),
                reaction_ok,
            )[1],
        )
        monkeypatch.setattr(
            tg, "edit_text",
            lambda cid, mid, text, **kw: self.edits.append(
                {"chat_id": cid, "message_id": mid, "text": text, "kw": kw}
            ),
        )
        monkeypatch.setattr(
            tg, "delete_message",
            lambda cid, mid: (self.deletes.append((cid, mid)), True)[1],
        )
        monkeypatch.setattr(db, "mark_dismissed_by_message", lambda cid, mid: True)


def _callback(data, *, chat_id=RENTALS_CHAT, text="🚨 Nieuw huis", message_id=42):
    return {
        "id": "cb",
        "data": data,
        "message": {
            "chat": {"id": int(chat_id)},
            "message_id": message_id,
            "text": text,
        },
    }


@pytest.mark.parametrize(
    "code,emoji,label",
    [("r", "✍", "gereageerd"), ("i", "🤝", "uitgenodigd"), ("x", "👎", "afgewezen")],
)
def test_status_button_sets_reaction(monkeypatch, code, emoji, label):
    rec = _Recorder(monkeypatch)
    responder._handle_callback(_callback(f"st:{code}"))
    assert rec.reactions == [(RENTALS_CHAT, 42, emoji)]
    assert rec.edits == []
    assert rec.answers == [f"Status: {label} {emoji}"]


def test_status_button_works_on_sales_chat(monkeypatch):
    rec = _Recorder(monkeypatch)
    responder._handle_callback(_callback("st:i", chat_id=SALES_CHAT))
    assert rec.reactions == [(SALES_CHAT, 42, "🤝")]
    assert rec.answers == ["Status: uitgenodigd 🤝"]


# ---------------------------------------------------------------------------
# Callback dispatch — reaction failure fallback (edit text)
# ---------------------------------------------------------------------------


def test_reaction_failure_falls_back_to_prefix_edit(monkeypatch):
    rec = _Recorder(monkeypatch, reaction_ok=False)
    responder._handle_callback(_callback("st:i", text="Voorstraat 1\nDelft"))
    assert len(rec.edits) == 1
    assert rec.edits[0]["text"] == "📅 Voorstraat 1\nDelft"
    assert rec.answers == ["Status: uitgenodigd 🤝"]


def test_reaction_failure_preserves_keyboard(monkeypatch):
    rec = _Recorder(monkeypatch, reaction_ok=False)
    cb = _callback("st:r")
    cb["message"]["reply_markup"] = {"inline_keyboard": [[{"text": "x"}]]}
    responder._handle_callback(cb)
    assert rec.edits[0]["kw"]["reply_markup"] == {"inline_keyboard": [[{"text": "x"}]]}


def test_repeated_press_replaces_previous_prefix(monkeypatch):
    rec = _Recorder(monkeypatch, reaction_ok=False)
    # First press prefixed ✅; a later ❌ press must replace it, not stack.
    responder._handle_callback(
        _callback("st:x", text="✅ Voorstraat 1\nDelft")
    )
    assert rec.edits[0]["text"] == "❌ Voorstraat 1\nDelft"


def test_strip_status_prefix_no_prefix_is_noop():
    assert responder._strip_status_prefix("Plain\nrest") == "Plain\nrest"


# ---------------------------------------------------------------------------
# Callback dispatch — dismiss
# ---------------------------------------------------------------------------


def test_dismiss_deletes_and_marks(monkeypatch):
    rec = _Recorder(monkeypatch)
    marked = []
    monkeypatch.setattr(
        db, "mark_dismissed_by_message",
        lambda cid, mid: (marked.append((cid, mid)), True)[1],
    )
    responder._handle_callback(_callback("st:d"))
    assert rec.deletes == [(RENTALS_CHAT, 42)]
    assert marked == [(RENTALS_CHAT, 42)]
    assert rec.answers == ["Verwijderd 🗑"]


def test_dismiss_on_sales_chat_still_deletes(monkeypatch):
    # No responder-owned row for koop messages; mark returns False, delete runs.
    rec = _Recorder(monkeypatch)
    monkeypatch.setattr(db, "mark_dismissed_by_message", lambda cid, mid: False)
    responder._handle_callback(_callback("st:d", chat_id=SALES_CHAT))
    assert rec.deletes == [(SALES_CHAT, 42)]
    assert rec.answers == ["Verwijderd 🗑"]


# ---------------------------------------------------------------------------
# Unknown chat / bad codes
# ---------------------------------------------------------------------------


def test_status_callback_from_unknown_chat_ignored(monkeypatch):
    rec = _Recorder(monkeypatch)
    responder._handle_callback(_callback("st:r", chat_id="-999"))
    assert rec.reactions == []
    assert rec.deletes == []
    assert rec.answers == [None]  # answered (to clear the spinner) then ignored


def test_unknown_status_code_answered(monkeypatch):
    rec = _Recorder(monkeypatch)
    responder._handle_callback(_callback("st:zzz"))
    assert rec.reactions == []
    assert rec.answers == [None]


# ---------------------------------------------------------------------------
# db.mark_dismissed_by_message
# ---------------------------------------------------------------------------


@pytest.fixture
def rdb(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "DB_PATH", str(tmp_path / "db.sqlite"))
    monkeypatch.setattr(db, "_local", threading.local())
    db.init_schema()
    c = db.conn()
    c.execute(
        "CREATE TABLE houses (url TEXT PRIMARY KEY, straatnaamHuisnummer TEXT, "
        "plaats TEXT)"
    )
    c.commit()
    return c


def _add(c, url="https://a.nl/1", tg_ids='{"-100": 42}', status="notified"):
    c.execute(
        "INSERT INTO houses (url, straatnaamHuisnummer, plaats) VALUES (?, ?, ?)",
        (url, "Voorstraat 1", "Delft"),
    )
    c.execute(
        "INSERT INTO responses (url, status, tg_message_ids, listing_status) "
        "VALUES (?, ?, ?, 'available')",
        (url, status, tg_ids),
    )
    c.commit()


def test_mark_dismissed_by_message_matches_and_updates(rdb):
    _add(rdb)
    assert db.mark_dismissed_by_message("-100", 42) is True
    row = rdb.execute("SELECT listing_status FROM responses").fetchone()
    assert row["listing_status"] == "dismissed"


def test_mark_dismissed_excludes_from_recheck(rdb):
    _add(rdb)
    db.mark_dismissed_by_message("-100", 42)
    assert db.available_listings(5) == []


def test_mark_dismissed_no_match_returns_false(rdb):
    _add(rdb)
    # Wrong message id (e.g. a koop message the responder never stored).
    assert db.mark_dismissed_by_message("-100", 999) is False
    row = rdb.execute("SELECT listing_status FROM responses").fetchone()
    assert row["listing_status"] == "available"


def test_mark_dismissed_ignores_empty_message_ids(rdb):
    _add(rdb, tg_ids="")
    assert db.mark_dismissed_by_message("-100", 42) is False
