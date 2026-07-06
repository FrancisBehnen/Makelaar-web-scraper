"""Unit tests for responder.detection helpers.

detection.py imports scrapling.fetchers (the browser stack) at module level.
Those extras aren't needed to exercise the pure-Python cookie parser, so we stub
the module before import to keep the test hermetic and fast.
"""

import sys
import types

# Stub scrapling.fetchers.StealthyFetcher so importing detection.py does not pull
# in curl_cffi / camoufox (the heavy browser dependencies).
if "scrapling.fetchers" not in sys.modules:
    scrapling_pkg = sys.modules.setdefault("scrapling", types.ModuleType("scrapling"))
    fetchers_mod = types.ModuleType("scrapling.fetchers")
    fetchers_mod.StealthyFetcher = object  # placeholder, never called in these tests
    scrapling_pkg.fetchers = fetchers_mod
    sys.modules["scrapling.fetchers"] = fetchers_mod

from detection import _parse_cookie_header  # noqa: E402


DOMAIN = ".huurstunt.nl"


def test_multiple_cookies():
    result = _parse_cookie_header("a=1; b=2; c=3", DOMAIN)
    assert [(c["name"], c["value"]) for c in result] == [("a", "1"), ("b", "2"), ("c", "3")]
    assert all(c["domain"] == DOMAIN and c["path"] == "/" for c in result)


def test_value_containing_equals():
    # JWT / base64 session values routinely contain '='. Only the first '='
    # separates name from value; the rest must stay in the value.
    result = _parse_cookie_header("session=abc==; token=x=y=z", DOMAIN)
    assert result[0] == {"name": "session", "value": "abc==", "domain": DOMAIN, "path": "/"}
    assert result[1]["value"] == "x=y=z"


def test_empty_string():
    assert _parse_cookie_header("", DOMAIN) == []


def test_whitespace_only():
    assert _parse_cookie_header("   ", DOMAIN) == []
    assert _parse_cookie_header("  ;  ;  ", DOMAIN) == []


def test_surrounding_and_internal_whitespace_trimmed():
    result = _parse_cookie_header("  a = 1 ;  b=2  ", DOMAIN)
    assert [(c["name"], c["value"]) for c in result] == [("a", "1"), ("b", "2")]


def test_segment_without_equals_skipped():
    result = _parse_cookie_header("valid=1; garbage; also_valid=2", DOMAIN)
    assert [c["name"] for c in result] == ["valid", "also_valid"]


def test_trailing_semicolon():
    result = _parse_cookie_header("a=1;", DOMAIN)
    assert [c["name"] for c in result] == ["a"]
