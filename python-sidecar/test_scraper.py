"""Unit tests for scraper's fetch-failure wedge guard.

scraper.py imports the scrapling browser stack at module level. Those extras
aren't needed to exercise the pure-Python guard logic, so we stub the modules
before import to keep the test hermetic and fast.
"""

import sys
import types

# Stub scrapling.fetchers / scrapling.parser so importing scraper.py does not
# pull in curl_cffi / camoufox (the heavy browser dependencies).
if "scrapling.fetchers" not in sys.modules:
    scrapling_pkg = sys.modules.setdefault("scrapling", types.ModuleType("scrapling"))
    fetchers_mod = types.ModuleType("scrapling.fetchers")
    fetchers_mod.StealthyFetcher = object  # placeholder, never called here
    parser_mod = types.ModuleType("scrapling.parser")
    parser_mod.Adaptor = object
    scrapling_pkg.fetchers = fetchers_mod
    scrapling_pkg.parser = parser_mod
    sys.modules["scrapling.fetchers"] = fetchers_mod
    sys.modules["scrapling.parser"] = parser_mod

import scraper  # noqa: E402


def _reset_guard(first_cycle_complete: bool) -> None:
    scraper._consecutive_fetch_failures = 0
    scraper._failed_urls_in_streak = set()
    scraper._first_cycle_complete = first_cycle_complete


def test_default_fetch_timeout_covers_cold_cloudflare():
    # A cold Cloudflare-turnstile fetch (measured ~127s on Funda) must fit
    # inside the default budget, or it times out ~7s before returning 200.
    assert scraper.FETCH_TIMEOUT >= 130


def test_wedge_guard_does_not_exit_during_first_cycle(monkeypatch):
    _reset_guard(first_cycle_complete=False)
    exited = []
    monkeypatch.setattr(scraper.os, "_exit", lambda code: exited.append(code))

    # Three distinct failing URLs would normally trip the guard.
    for u in ("a", "b", "c", "d"):
        scraper._record_fetch_failure(u, "timeout (worker stuck)")

    assert exited == []  # never self-restarts before one cycle completes
    assert scraper._consecutive_fetch_failures >= scraper.MAX_CONSECUTIVE_FETCH_FAILURES


def test_wedge_guard_exits_after_first_cycle(monkeypatch, tmp_path):
    _reset_guard(first_cycle_complete=True)
    exited = []
    monkeypatch.setattr(scraper.os, "_exit", lambda code: exited.append(code))
    monkeypatch.setattr(scraper, "send_telegram_alert", lambda msg: None)
    # Redirect the self-restart marker into a temp dir instead of the CWD.
    monkeypatch.setattr(scraper, "SELF_RESTART_MARKER", tmp_path / ".self_restart")

    for u in ("a", "b", "c"):
        scraper._record_fetch_failure(u, "timeout")

    assert exited == [1]  # genuine wedge on a warm browser self-restarts


def test_repeated_same_url_is_not_counted_as_wedge(monkeypatch):
    _reset_guard(first_cycle_complete=True)
    exited = []
    monkeypatch.setattr(scraper.os, "_exit", lambda code: exited.append(code))

    # One sick site failing repeatedly is not a wedged browser.
    for _ in range(5):
        scraper._record_fetch_failure("same-url", "timeout")

    assert exited == []
    assert scraper._consecutive_fetch_failures == 1


# ---------------------------------------------------------------------------
# Junk filter (non-dwelling listings)
# ---------------------------------------------------------------------------

import pytest  # noqa: E402


@pytest.mark.parametrize(
    "title,expected",
    [
        ("Bedrijfspand Voorstraat 12", True),
        ("Kantoorruimte Markt 5", True),
        ("Bedrijfsruimte Delftweg 3", True),
        ("Bedrijfshal Industrieweg 7", True),
        ("Winkelruimte Choorstraat 2", True),
        ("Praktijkruimte Oude Delft 1", True),
        ("Horeca Beestenmarkt 9", True),
        ("Parkeerplaats 42", True),
        ("Garagebox Voorstraat", True),
        ("Voorstraat 1", False),
        ("Garagepad 4", False),
    ],
)
def test_is_junk_listing(title, expected):
    house = {"straatnaamHuisnummer": title}
    assert scraper.is_junk_listing(house) is expected
