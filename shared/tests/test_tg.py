"""Direct unit tests for the shared Telegram helpers + client."""

import sys
import types

from shared import tg


# ---------------------------------------------------------------------------
# Pure helpers
# ---------------------------------------------------------------------------


def test_escape_html():
    assert tg.escape_html("a & b < c > d") == "a &amp; b &lt; c &gt; d"
    assert tg.escape_html(None) == ""


def test_status_button_row_shape():
    assert tg.status_button_row() == [
        {"text": "✅", "callback_data": "st:r"},
        {"text": "📅", "callback_data": "st:i"},
        {"text": "❌", "callback_data": "st:x"},
        {"text": "🗑", "callback_data": "st:d"},
    ]


def test_status_button_row_is_fresh_each_call():
    a = tg.status_button_row()
    a[0]["text"] = "MUTATED"
    assert tg.status_button_row()[0]["text"] == "✅"


def test_status_keyboard_single_row():
    kb = tg.status_keyboard()
    assert kb == {"inline_keyboard": [tg.status_button_row()]}


# ---------------------------------------------------------------------------
# TelegramClient (with a stubbed ``requests`` module)
# ---------------------------------------------------------------------------


class _FakeResp:
    def __init__(self, payload):
        self._payload = payload

    def json(self):
        return self._payload


def _install_requests(monkeypatch, capture, payload):
    fake = types.ModuleType("requests")

    def post(url, **kwargs):
        capture.append((url, kwargs))
        return _FakeResp(payload)

    fake.post = post
    monkeypatch.setitem(sys.modules, "requests", fake)


def test_client_no_token_drops_call(monkeypatch):
    capture = []
    _install_requests(monkeypatch, capture, {"ok": True, "result": {}})
    client = tg.TelegramClient("")
    assert client.send_message("-100", "hi") is None
    assert capture == []  # never hit the network


def test_client_send_message_returns_id(monkeypatch):
    capture = []
    _install_requests(monkeypatch, capture, {"ok": True, "result": {"message_id": 7}})
    client = tg.TelegramClient("tok")
    assert client.send_message("-100", "hi", reply_markup={"x": 1}) == 7
    url, kwargs = capture[0]
    assert url.endswith("/sendMessage")
    assert kwargs["json"]["reply_markup"] == {"x": 1}


def test_client_delete_message_tolerates_rejection(monkeypatch):
    capture = []
    _install_requests(monkeypatch, capture, {"ok": False, "description": "too old"})
    client = tg.TelegramClient("tok")
    assert client.delete_message("-100", 5) is False


def test_client_set_reaction_true_on_ok(monkeypatch):
    capture = []
    _install_requests(monkeypatch, capture, {"ok": True, "result": True})
    client = tg.TelegramClient("tok")
    assert client.set_reaction("-100", 5, "✍") is True
