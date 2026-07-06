"""Tests for the Delft koop sales scraper."""

import pytest

import sales_scraper as s


def _house(**overrides) -> dict[str, str]:
    base = {
        "url": "https://example.com/a",
        "straatnaamHuisnummer": "Voorstraat 1",
        "plaats": "Delft",
        "vraagprijs": "€ 250.000 k.k.",
        "oppervlakte": "80 m²",
        "kamers": "3 kamers",
    }
    base.update(overrides)
    return base


# ---------------------------------------------------------------------------
# is_delft_city
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "text,expected",
    [
        ("Delft", True),
        ("delft", True),
        ("2611 AB Delft", True),
        ("2611 AB Delft (Binnenstad)", True),
        ("Delfgauw", False),
        ("Den Hoorn", False),
        ("Rijswijk", False),
        ("Delftstraat", False),  # word-boundary guard
        ("", False),
        (None, False),
    ],
)
def test_is_delft_city(text, expected):
    assert s.is_delft_city(text) is expected


# ---------------------------------------------------------------------------
# parse_rooms
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "text,expected",
    [
        ("3 kamers", 3),
        ("2", 2),
        ("1 kamer", 1),
        ("5 slaapkamers", 5),
        ("", None),
        ("kamers", None),
        (None, None),
    ],
)
def test_parse_rooms(text, expected):
    assert s.parse_rooms(text) == expected


# ---------------------------------------------------------------------------
# passes_filters
# ---------------------------------------------------------------------------


def test_passes_filters_typical():
    assert s.passes_filters(_house()) is True


def test_price_boundary_inclusive():
    assert s.passes_filters(_house(vraagprijs="€ 270.000 k.k.")) is True


def test_price_above_max_excluded():
    assert s.passes_filters(_house(vraagprijs="€ 270.001 k.k.")) is False


def test_missing_price_excluded():
    assert s.passes_filters(_house(vraagprijs="")) is False
    assert s.passes_filters(_house(vraagprijs="op aanvraag")) is False


def test_one_room_excluded():
    assert s.passes_filters(_house(kamers="1 kamer")) is False


def test_unknown_rooms_kept():
    assert s.passes_filters(_house(kamers="")) is True


def test_studio_excluded():
    assert (
        s.passes_filters(
            _house(straatnaamHuisnummer="Studio Voorstraat 1", kamers="")
        )
        is False
    )


def test_non_delft_city_excluded():
    assert s.passes_filters(_house(plaats="Rijswijk")) is False
    assert s.passes_filters(_house(plaats="Delfgauw")) is False


# ---------------------------------------------------------------------------
# DB / dedup / seed / restart
# ---------------------------------------------------------------------------


@pytest.fixture()
def db(tmp_path, monkeypatch):
    monkeypatch.setattr(s, "DB_PATH", str(tmp_path / "sales.sqlite"))
    conn = s.init_db()
    yield conn
    conn.close()


@pytest.fixture()
def notifications(monkeypatch):
    sent: list[dict[str, str]] = []
    monkeypatch.setattr(s, "notify_new_listing", lambda h: sent.append(h))
    return sent


def test_seed_on_first_run_no_notify(db, notifications):
    assert s.table_is_empty(db) is True
    houses = [
        _house(url="https://a.nl/1"),
        _house(url="https://b.nl/2", straatnaamHuisnummer="Achterstraat 9"),
    ]
    existing = s.get_existing_urls(db)
    notified = s.process_houses(db, houses, existing, seeding=True)
    assert notified == 0
    assert notifications == []
    assert db.execute("SELECT COUNT(*) FROM sales").fetchone()[0] == 2


def test_new_listing_notifies(db, notifications):
    existing = s.get_existing_urls(db)
    s.process_houses(db, [_house(url="https://a.nl/1")], existing, seeding=False)
    assert len(notifications) == 1


def test_cross_source_dedup_notifies_once(db, notifications):
    # Same apartment (address + city), two different source URLs.
    h1 = _house(url="https://funda.nl/x", straatnaamHuisnummer="Markt 3 A")
    h2 = _house(url="https://pararius.nl/y", straatnaamHuisnummer="Markt 3A")
    existing = s.get_existing_urls(db)
    s.process_houses(db, [h1, h2], existing, seeding=False)
    assert len(notifications) == 1
    assert db.execute("SELECT COUNT(*) FROM sales").fetchone()[0] == 2


def test_dedup_across_postal_city_variants(db, notifications):
    h1 = _house(url="https://a.nl/1", plaats="Delft")
    h2 = _house(url="https://b.nl/2", plaats="2611 AB Delft")
    existing = s.get_existing_urls(db)
    s.process_houses(db, [h1, h2], existing, seeding=False)
    assert len(notifications) == 1


def test_restart_does_not_renotify(db, notifications):
    h = _house(url="https://a.nl/1")
    existing = s.get_existing_urls(db)
    s.process_houses(db, [h], existing, seeding=False)
    assert len(notifications) == 1

    # Simulate a restart: reload existing URLs from the DB, scrape again.
    reloaded = s.get_existing_urls(db)
    s.process_houses(db, [h], reloaded, seeding=False)
    assert len(notifications) == 1  # no second notification


def test_distinct_addresses_both_notify(db, notifications):
    h1 = _house(url="https://a.nl/1", straatnaamHuisnummer="Markt 3")
    h2 = _house(url="https://b.nl/2", straatnaamHuisnummer="Markt 4")
    existing = s.get_existing_urls(db)
    s.process_houses(db, [h1, h2], existing, seeding=False)
    assert len(notifications) == 2


# ---------------------------------------------------------------------------
# Parser smoke tests (need scrapling's Adaptor)
# ---------------------------------------------------------------------------


def _adaptor(html: str, url: str):
    scrapling_parser = pytest.importorskip("scrapling.parser")
    return scrapling_parser.Adaptor(html, url=url)


PARARIUS_HTML = """
<ul>
  <li class="search-list__item--listing">
    <a class="listing-search-item__link--title"
       href="/appartement-te-koop/delft/abc123/voorstraat-1">Voorstraat 1</a>
    <div class="listing-search-item__sub-title">2611 AB Delft</div>
    <div class="listing-search-item__price-main">&euro; 250.000 k.k.</div>
    <li class="illustrated-features__item--surface-area">75 m&sup2;</li>
    <li class="illustrated-features__item--number-of-rooms">3 kamers</li>
  </li>
</ul>
"""


def test_scrape_pararius_koop_fixture():
    page = _adaptor(PARARIUS_HTML, "https://www.pararius.nl/koopwoningen/delft")
    houses = s.scrape_pararius_koop(page)
    assert len(houses) == 1
    h = houses[0]
    assert h["url"] == (
        "https://www.pararius.nl/appartement-te-koop/delft/abc123/voorstraat-1"
    )
    assert h["straatnaamHuisnummer"] == "Voorstraat 1"
    assert s.is_delft_city(h["plaats"])
    assert "250.000" in h["vraagprijs"]
    assert s.passes_filters(h)


REALWORKS_HTML = """
<ul>
  <li class="aanbodEntry">
    <a class="aanbodEntryLink"
       href="/aanbod/woningaanbod/koop/voorstraat-1/">x</a>
    <h3 class="street-address">Voorstraat 1</h3>
    <span class="locality">Delft</span>
    <span class="kenmerkValue">&euro; 260.000 k.k.</span>
    <div>75 m&sup2; &middot; 3 kamers</div>
  </li>
  <li class="aanbodEntry">
    <a class="aanbodEntryLink"
       href="/aanbod/woningaanbod/huur/achterstraat-9/">y</a>
    <h3 class="street-address">Achterstraat 9</h3>
    <span class="locality">Delft</span>
    <span class="kenmerkValue">&euro; 1.400</span>
  </li>
</ul>
"""


def test_scrape_realworks_koop_status_gate():
    page = _adaptor(REALWORKS_HTML, "https://www.zomakelaars.nl/aanbod")
    houses = s.scrape_zomakelaars_koop(page)
    # The /huur/ entry must be dropped by the inverted status gate.
    assert len(houses) == 1
    assert "/koop/" in houses[0]["url"]
    assert houses[0]["straatnaamHuisnummer"] == "Voorstraat 1"
