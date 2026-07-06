"""Routing tests: a URL from the sales chat opens a sales-targeted add-site
issue, a URL from the rentals chat keeps the existing (rental) behaviour, and
neither path touches the other's logic.

responder.py imports detection (scrapling) and form_filler (camoufox) at module
level. Those browser stacks aren't needed here, so we stub them before import,
mirroring test_detection.py.
"""

import sys
import types

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
import github_issues  # noqa: E402
import responder  # noqa: E402
import tg  # noqa: E402

RENTALS_CHAT = "-100"
SALES_CHAT = "-200"


class _Harness:
    """In-memory kv store + captured issue-creation calls."""

    def __init__(self, monkeypatch):
        self.store: dict[str, str] = {}
        self.issue_calls: list[dict] = []
        self.messages: list[dict] = []

        monkeypatch.setattr(config, "TELEGRAM_CHAT_IDS", [RENTALS_CHAT])
        monkeypatch.setattr(config, "TELEGRAM_SALES_CHAT_IDS", [SALES_CHAT])
        monkeypatch.setattr(db, "kv_set", self.store.__setitem__)
        monkeypatch.setattr(db, "kv_get", self.store.get)
        monkeypatch.setattr(db, "kv_delete", lambda k: self.store.pop(k, None))
        monkeypatch.setattr(
            tg,
            "send_message",
            lambda chat_id, text, **kw: self.messages.append(
                {"chat_id": chat_id, "text": text}
            )
            or 1,
        )
        monkeypatch.setattr(tg, "answer_callback", lambda *a, **k: None)
        monkeypatch.setattr(
            github_issues,
            "create_add_site_issue",
            lambda url, *, sales=False: self.issue_calls.append(
                {"url": url, "sales": sales}
            )
            or "https://gh/issue/x",
        )

    def token(self) -> str:
        keys = [k for k in self.store if k.startswith("addsite:")]
        assert len(keys) == 1, keys
        return keys[0].split(":", 1)[1]

    def confirm(self, chat_id: str) -> None:
        responder._handle_callback(
            {
                "id": "cb",
                "data": f"siteok:{self.token()}",
                "message": {"chat": {"id": int(chat_id)}},
            }
        )


def test_sales_chat_url_creates_sales_issue(monkeypatch):
    h = _Harness(monkeypatch)
    responder._handle_message(
        {"chat": {"id": int(SALES_CHAT)}, "text": "check https://foo.nl/koop/1"}
    )
    h.confirm(SALES_CHAT)
    assert h.issue_calls == [{"url": "https://foo.nl/koop/1", "sales": True}]


def test_rentals_chat_url_creates_rental_issue(monkeypatch):
    h = _Harness(monkeypatch)
    responder._handle_message(
        {"chat": {"id": int(RENTALS_CHAT)}, "text": "check https://foo.nl/huis/1"}
    )
    h.confirm(RENTALS_CHAT)
    assert h.issue_calls == [{"url": "https://foo.nl/huis/1", "sales": False}]


def test_unknown_chat_ignored(monkeypatch):
    h = _Harness(monkeypatch)
    responder._handle_message(
        {"chat": {"id": -999}, "text": "check https://foo.nl/koop/1"}
    )
    assert h.store == {}
    assert h.messages == []


def test_sales_chat_does_not_trigger_rental_listing_callbacks(monkeypatch):
    # brief/fill/ok/no callbacks operate on response ids; a sales chat should
    # not resolve them into rental listing actions. With no such response the
    # handler must no-op gracefully (no issue, no crash).
    h = _Harness(monkeypatch)
    monkeypatch.setattr(db, "get_response", lambda rid: None)
    responder._handle_callback(
        {
            "id": "cb",
            "data": "fill:123",
            "message": {"chat": {"id": int(SALES_CHAT)}},
        }
    )
    assert h.issue_calls == []
