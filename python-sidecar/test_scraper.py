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


# ---------------------------------------------------------------------------
# Momento (Presendoo SPA — api.presendoo.app JSON, no HTML rendering)
# ---------------------------------------------------------------------------

import json  # noqa: E402


def _momento_unit(uuid, name, price, availability="available"):
    return {
        "uuid": uuid,
        "name": name,
        "price": price,
        "availability": availability,
    }


def _momento_fields_entry(unit_uuid, rooms=None, area=None):
    fields = []
    if rooms is not None:
        fields.append(
            {"external_identifier": "numberOfRooms", "value": {"raw": str(rooms)}}
        )
    if area is not None:
        fields.append(
            {"external_identifier": "livingArea", "value": {"raw": str(area)}}
        )
    return {"unit_uuid": unit_uuid, "fields": fields}


def _momento_fake_http_get(units, fields_entries):
    """Route the three plain-HTTP calls scrape_momento_via_http makes:
    project-by-prefix lookup, the bulk units list, and the bulk per-unit
    fields feed (bedroom count / living area)."""

    def fake(url, timeout=30, cookie=None):
        if "by-prefix" in url:
            return json.dumps({"uuid": "proj-uuid"}).encode()
        if "/units/fields" in url:
            return json.dumps({"data": fields_entries, "meta": {}}).encode()
        if url.endswith("/units"):
            return json.dumps({"data": units, "meta": {}}).encode()
        raise AssertionError(f"unexpected Momento URL: {url}")

    return fake


def test_momento_keeps_available_within_price_budget(monkeypatch):
    units = [
        _momento_unit("u1", "Prinses Alexia Promenade 5", "1265.5700"),
        _momento_unit("u2", "Prinses Alexia Promenade 7", "2095.0000"),  # over MAX_PRICE
        _momento_unit(
            "u3", "Prinses Alexia Promenade 9", "1230.0500", availability="in_option"
        ),
        _momento_unit(
            "u4", "Prinses Alexia Promenade 11", "1101.6500", availability="sold"
        ),
    ]
    fields_entries = [
        _momento_fields_entry("u1", rooms=0, area=33),
        _momento_fields_entry("u2", rooms=1, area=45),
        _momento_fields_entry("u3", rooms=1, area=45),
        _momento_fields_entry("u4", rooms=1, area=45),
    ]
    monkeypatch.setattr(scraper, "_http_get", _momento_fake_http_get(units, fields_entries))

    houses = scraper.scrape_momento_via_http(existing_urls=set())

    assert len(houses) == 1
    h = houses[0]
    assert h["url"] == f"{scraper.MOMENTO_BASE_URL}/units/u1"
    assert h["straatnaamHuisnummer"] == "Prinses Alexia Promenade 5"
    assert h["plaats"] == "Rijswijk"
    assert h["vraagprijs"] == "€ 1.266 p.m."
    assert h["oppervlakte"] == "33 m²"
    assert h["kamers"] == "Studio"
    assert scraper.is_delft_area(h["plaats"]) is True


def test_momento_skips_known_urls(monkeypatch):
    units = [_momento_unit("u1", "Prinses Alexia Promenade 5", "1265.5700")]
    fields_entries = [_momento_fields_entry("u1", rooms=0, area=33)]
    monkeypatch.setattr(scraper, "_http_get", _momento_fake_http_get(units, fields_entries))

    existing = {f"{scraper.MOMENTO_BASE_URL}/units/u1"}
    houses = scraper.scrape_momento_via_http(existing_urls=existing)

    assert houses == []


def test_momento_bedroom_count_formats_kamers_field(monkeypatch):
    units = [_momento_unit("u2", "Steenvoordelaan 402 F013", "1265.5700")]
    fields_entries = [_momento_fields_entry("u2", rooms=2, area=60)]
    monkeypatch.setattr(scraper, "_http_get", _momento_fake_http_get(units, fields_entries))

    houses = scraper.scrape_momento_via_http(existing_urls=set())

    assert houses[0]["kamers"] == "2 slaapkamers"


def test_momento_project_lookup_failure_returns_empty(monkeypatch):
    def fake(url, timeout=30, cookie=None):
        raise OSError("boom")

    monkeypatch.setattr(scraper, "_http_get", fake)

    assert scraper.scrape_momento_via_http(existing_urls=set()) == []


def test_momento_units_fetch_failure_returns_empty(monkeypatch):
    def fake(url, timeout=30, cookie=None):
        if "by-prefix" in url:
            return json.dumps({"uuid": "proj-uuid"}).encode()
        raise OSError("boom")

    monkeypatch.setattr(scraper, "_http_get", fake)

    assert scraper.scrape_momento_via_http(existing_urls=set()) == []
