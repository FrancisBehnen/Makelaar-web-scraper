"""Tests for the Delft koop sales scraper."""

import json
import sqlite3

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
        s.passes_filters(_house(straatnaamHuisnummer="Studio Voorstraat 1", kamers=""))
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

    def _fake_notify(h):
        sent.append(h)
        return []

    monkeypatch.setattr(s, "notify_new_listing", _fake_notify)
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


# ---------------------------------------------------------------------------
# Room semantics: kamers vs slaapkamers normalisation
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "bedrooms,expected",
    [
        (0, "1 kamer"),  # studio: 0 bedrooms -> 1 kamer
        (1, "2 kamers"),  # 1 bedroom apart from living room -> 2-kamer
        (2, "3 kamers"),
        (4, "5 kamers"),
    ],
)
def test_bedrooms_to_kamers(bedrooms, expected):
    assert s.bedrooms_to_kamers(bedrooms) == expected


def test_one_bedroom_flat_passes_after_normalisation():
    # A 2-kamer (1 slaapkamer) apartment is exactly what the user wants kept.
    assert s.passes_filters(_house(kamers="2 kamers")) is True


def test_studio_one_kamer_excluded():
    assert s.passes_filters(_house(kamers="1 kamer")) is False


# Funda cards render *bedrooms* (a bare number next to a bed icon), so the
# parser must add the living room back before applying the >= 2 gate.
FUNDA_HTML = """
<div>
  <a data-testid="listingDetailsAddress" href="/koop/delft/huis-1/">
    <span class="truncate">Voorstraat 1</span>
    <span class="text-neutral-80">2611 AB Delft</span>
  </a>
  <div><div class="font-semibold"><div class="truncate">
    &euro; 250.000 k.k.</div></div></div>
  <ul><li><span>63 m&sup2;</span></li><li><span>1</span></li>
      <li><span>A</span></li></ul>
</div>
"""


def test_funda_koop_bedrooms_normalised_to_kamers():
    page = _adaptor(FUNDA_HTML, "https://www.funda.nl")
    houses = s.scrape_funda_koop(page)
    assert len(houses) == 1
    h = houses[0]
    # Funda showed "1" (bedroom) -> stored as "2 kamers" so it passes >= 2.
    assert h["kamers"] == "2 kamers"
    assert s.passes_filters(h) is True


FUNDA_STUDIO_HTML = """
<div>
  <a data-testid="listingDetailsAddress" href="/koop/delft/huis-2/">
    <span class="truncate">Achterstraat 9</span>
    <span class="text-neutral-80">2611 AB Delft</span>
  </a>
  <div><div class="font-semibold"><div class="truncate">
    &euro; 190.000 k.k.</div></div></div>
  <ul><li><span>32 m&sup2;</span></li><li><span>0</span></li>
      <li><span>C</span></li></ul>
</div>
"""


def test_funda_koop_studio_zero_bedrooms_excluded():
    page = _adaptor(FUNDA_STUDIO_HTML, "https://www.funda.nl")
    houses = s.scrape_funda_koop(page)
    assert len(houses) == 1
    assert houses[0]["kamers"] == "1 kamer"
    assert s.passes_filters(houses[0]) is False


# ---------------------------------------------------------------------------
# Realworks "Aantal kamers N" (number *after* the label) + saleprice themes
# ---------------------------------------------------------------------------

REALWORKS_KAMERS_HTML = """
<ul>
  <li class="al2woning aanbodEntry">
    <a class="aanbodEntryLink"
       href="/aanbod/woningaanbod/delft/koop/huis-1-voorstraat-1/">x</a>
    <h3 class="street-address">Voorstraat 1</h3>
    <span class="locality">Delft</span>
    <span class="kenmerkValue">&euro; 260.000,- k.k.</span>
    <span class="kenmerkValue">Appartement</span>
    <span class="kenmerkValue">63 m&sup2;</span>
    <span class="kenmerkValue">3</span>
    <div>Woonoppervlakte 63 m&sup2; Aantal kamers 3</div>
  </li>
  <li class="al2woning aanbodEntry">
    <a class="aanbodEntryLink"
       href="/aanbod/woningaanbod/delft/koop/huis-2-studioweg-2/">y</a>
    <h3 class="street-address">Studioweg 2</h3>
    <span class="locality">Delft</span>
    <span class="kenmerkValue">&euro; 180.000,- k.k.</span>
    <div>Woonoppervlakte 30 m&sup2; Aantal kamers 1</div>
  </li>
</ul>
"""


def test_realworks_aantal_kamers_parsed_as_total():
    page = _adaptor(REALWORKS_KAMERS_HTML, "https://www.zomakelaars.nl")
    houses = s._scrape_realworks_koop(page, "https://www.zomakelaars.nl", "ZO")
    assert len(houses) == 2
    by_addr = {h["straatnaamHuisnummer"]: h for h in houses}
    three = by_addr["Voorstraat 1"]
    assert three["kamers"] == "3 kamers"
    assert three["oppervlakte"] == "63 m²"
    assert "260.000" in three["vraagprijs"]
    assert s.passes_filters(three) is True
    # "Aantal kamers 1" -> 1 kamer studio -> excluded by the >= 2 gate.
    one = by_addr["Studioweg 2"]
    assert one["kamers"] == "1 kamer"
    assert s.passes_filters(one) is False


# Roepman theme: no kenmerkValue price, price lives in span.saleprice.
ROEPMAN_HTML = """
<div class="blok objectblok aanbodEntry">
  <a class="aanbodEntryLink"
     href="/aanbod/woningaanbod/delft/koop/huis-3-groene-zoom-14/">x</a>
  <h3 class="street-address">Groene Zoom 14</h3>
  <span class="locality">Delft</span>
  <span class="price prijs"><span class="saleprice">&euro; 265.000,- k.k.</span></span>
</div>
"""


def test_realworks_roepman_saleprice_and_div_entry():
    page = _adaptor(ROEPMAN_HTML, "https://www.roepman.nl")
    houses = s._scrape_realworks_koop(page, "https://www.roepman.nl", "Roepman")
    assert len(houses) == 1
    h = houses[0]
    assert h["straatnaamHuisnummer"] == "Groene Zoom 14"
    assert "265.000" in h["vraagprijs"]
    # No room count on Roepman's list card -> kept (unknown rooms).
    assert h["kamers"] == ""
    assert s.passes_filters(h) is True


# ---------------------------------------------------------------------------
# Junk filter (parking, garages, storage, plots)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "title,is_junk",
    [
        ("Artemisstraat parkeerplaats 42", True),
        ("Parkeerplek 7", True),
        ("Garagebox Voorstraat", True),
        ("Garage naast nr 3", True),
        ("Berging 12", True),
        ("Bouwgrond Delftweg", True),
        ("Kavel 5", True),
        ("Opslag unit 9", True),
        ("Voorstraat 1", False),
        ("Garagepad 4", False),  # word-boundary: not "garage"
        ("Bergingang 2", False),  # not "berging" as a whole word
    ],
)
def test_is_junk_listing(title, is_junk):
    assert s.is_junk_listing(title) is is_junk


def test_parking_spot_excluded_by_filter():
    parking = _house(
        straatnaamHuisnummer="Artemisstraat parkeerplaats 42",
        vraagprijs="€ 16.500 k.k.",
        kamers="",
    )
    assert s.passes_filters(parking) is False


def test_building_plot_excluded_by_filter():
    plot = _house(
        straatnaamHuisnummer="Bouwgrond Delftweg 1",
        vraagprijs="€ 36.500 k.k.",
        kamers="",
    )
    assert s.passes_filters(plot) is False


# ---------------------------------------------------------------------------
# Realtime-listings JSON feed (Van Daal / Björnd)
# ---------------------------------------------------------------------------


def _feed_entry(**overrides):
    base = {
        "address": "Voorstraat 1",
        "city": "Delft",
        "url": "/nl/aanbod/koop/delft/appartement/voorstraat-1/abc",
        "price": "&euro; 250.000 k.k.",
        "salesPrice": 250000,
        "rentalsPrice": 0,
        "livingSurface": 80,
        "rooms": 3,
        "bedrooms": 2,
        "isSales": True,
        "isRentals": False,
        "statusOrig": "available",
    }
    base.update(overrides)
    return base


def test_realtime_feed_keeps_available_sales(monkeypatch):
    entries = [
        _feed_entry(),  # available Delft sale -> kept
        _feed_entry(statusOrig="sold", address="Sold St 2"),  # sold -> dropped
        _feed_entry(  # rental-only -> dropped
            isSales=False, isRentals=True, address="Rental St 3"
        ),
    ]
    monkeypatch.setattr(
        s, "_http_get", lambda url, timeout=30: json.dumps(entries).encode()
    )
    houses = s._scrape_realtime_listings_sales(
        "https://feed", "https://vandaal.nl", "Van Daal"
    )
    assert len(houses) == 1
    h = houses[0]
    assert (
        h["url"]
        == "https://vandaal.nl/nl/aanbod/koop/delft/appartement/voorstraat-1/abc"
    )
    # rooms (total kamers), not bedrooms, is stored.
    assert h["kamers"] == "3 kamers"
    assert h["vraagprijs"] == "€ 250.000 k.k."
    assert s.passes_filters(h) is True


def test_realtime_feed_uses_rooms_not_bedrooms(monkeypatch):
    # A 2-kamer flat: rooms=2, bedrooms=1. Must store total kamers (2), which
    # passes; storing bedrooms (1) would wrongly fail the >= 2 gate.
    entries = [_feed_entry(rooms=2, bedrooms=1, salesPrice=240000)]
    monkeypatch.setattr(
        s, "_http_get", lambda url, timeout=30: json.dumps(entries).encode()
    )
    houses = s._scrape_realtime_listings_sales("u", "https://b.nl", "B")
    assert houses[0]["kamers"] == "2 kamers"
    assert s.passes_filters(houses[0]) is True


def test_realtime_feed_parking_dropped_by_junk_filter(monkeypatch):
    entries = [
        _feed_entry(
            address="Artemisstraat parkeerplaats 42",
            salesPrice=16500,
            rooms=0,
            bedrooms=0,
        )
    ]
    monkeypatch.setattr(
        s, "_http_get", lambda url, timeout=30: json.dumps(entries).encode()
    )
    houses = s._scrape_realtime_listings_sales("u", "https://b.nl", "B")
    # The feed helper returns it (available sale); passes_filters rejects it.
    assert len(houses) == 1
    assert s.passes_filters(houses[0]) is False


# ---------------------------------------------------------------------------
# Prinsenstad koop detail (Hayweb sale) — sold gate + kamers
# ---------------------------------------------------------------------------


def _prinsenstad_detail(status: str, header: str) -> bytes:
    return f"""
    <html><body>
      <h1>{header}</h1>
      <table class="feautures">
        <tr><td class="object_detail_title">Vraagprijs</td>
            <td>&euro; 260.000,- k.k.</td></tr>
        <tr><td class="object_detail_title">Status</td>
            <td>{status}</td></tr>
        <tr><td class="object_detail_title">Woonoppervlakte</td>
            <td>96 m&sup2;</td></tr>
        <tr><td class="object_detail_title">Aantal kamers</td>
            <td>4 (waarvan 3 slaapkamers)</td></tr>
      </table>
    </body></html>
    """.encode()


def test_prinsenstad_koop_available_kept():
    pytest.importorskip("scrapling.parser")
    body = _prinsenstad_detail("Beschikbaar", "Te koop: Voorstraat 1, 2611 AB Delft")
    h = s._parse_prinsenstad_koop_listing(
        "https://prinsenstadmakelaardij.nl/woningaanbod/koop/delft/voorstraat/1",
        body,
    )
    assert h is not None
    assert h["straatnaamHuisnummer"] == "Voorstraat 1"
    assert h["plaats"] == "Delft"
    assert h["kamers"] == "4 kamers"  # total kamers from "4 (waarvan 3 ...)"
    assert s.passes_filters(h) is True


def test_prinsenstad_koop_sold_skipped():
    pytest.importorskip("scrapling.parser")
    body = _prinsenstad_detail("Verkocht", "Verkocht: Voorstraat 1, 2611 AB Delft")
    h = s._parse_prinsenstad_koop_listing(
        "https://prinsenstadmakelaardij.nl/woningaanbod/koop/delft/voorstraat/1",
        body,
    )
    assert h is None


# ---------------------------------------------------------------------------
# Olsthoorn Makelaars koop grid (custom WordPress "Sure" plugin)
# ---------------------------------------------------------------------------
# Cards nest the m² digit in a <sup>, and total kamers is already the door-icon
# number (cross-checked against a real detail page's "N (waarvan M
# slaapkamers)" — no bedrooms_to_kamers conversion needed here).

OLSTHOORN_GRID_HTML = """
<div class="section--houses">
  <div class="house--col">
    <a href="https://www.olsthoornmakelaars.nl/wonen/object/voorstraat-1-delft/"
       class="card-house">
      <div class="card-house__thumb">
        <div class="card-house__status">
          <span class="card-house__label badge-available">Beschikbaar</span>
        </div>
      </div>
      <div class="short--info">
        <h2 class="h5 card__title">Delft</h2>
        <p>Voorstraat
          1
        </p>
        <p><b>&euro;
          250.000
          k.k.</b></p>
        <div class="data--short">
          <div class="data">
            <span class="icon"><i class="icon-sizes"></i></span>
            <span class="date__inner">80 m<sup>2</sup></span>
          </div>
          <div class="data">
            <span class="icon"><i class="icon-door"></i></span>
            <span class="date__inner">3
              kamers</span>
          </div>
        </div>
      </div>
    </a>
  </div>
  <div class="house--col">
    <a href="https://www.olsthoornmakelaars.nl/wonen/object/achterstraat-9-delft/"
       class="card-house">
      <div class="card-house__thumb">
        <div class="card-house__status">
          <span class="card-house__label badge-sold">Verkocht</span>
        </div>
      </div>
      <div class="short--info">
        <h2 class="h5 card__title">Delft</h2>
        <p>Achterstraat 9</p>
        <p><b>&euro; 260.000 k.k.</b></p>
      </div>
    </a>
  </div>
  <div class="house--col">
    <a href="https://www.olsthoornmakelaars.nl/wonen/object/dorpsstraat-3-onder-bod/"
       class="card-house">
      <div class="card-house__thumb">
        <div class="card-house__status">
          <span class="card-house__label badge">Onder bod</span>
        </div>
      </div>
      <div class="short--info">
        <h2 class="h5 card__title">Delft</h2>
        <p>Dorpsstraat 3</p>
        <p><b>&euro; 240.000 k.k.</b></p>
      </div>
    </a>
  </div>
  <div class="house--col">
    <a href="https://www.olsthoornmakelaars.nl/wonen/object/kerkstraat-2-rijswijk/"
       class="card-house">
      <div class="card-house__thumb">
        <div class="card-house__status">
          <span class="card-house__label badge-available">Beschikbaar</span>
        </div>
      </div>
      <div class="short--info">
        <h2 class="h5 card__title">Rijswijk</h2>
        <p>Kerkstraat 2</p>
        <p><b>&euro; 230.000 k.k.</b></p>
      </div>
    </a>
  </div>
</div>
"""


def test_olsthoorn_card_available_delft_parsed():
    page = _adaptor(OLSTHOORN_GRID_HTML, "https://www.olsthoornmakelaars.nl/wonen/")
    card = page.css("a.card-house")[0]
    h = s._parse_olsthoorn_card(card)
    assert h is not None
    assert h["url"] == (
        "https://www.olsthoornmakelaars.nl/wonen/object/voorstraat-1-delft/"
    )
    assert h["straatnaamHuisnummer"] == "Voorstraat 1"
    assert h["plaats"] == "Delft"
    assert "250.000" in h["vraagprijs"]
    assert h["kamers"] == "3 kamers"
    assert h["oppervlakte"] == "80 m²"  # nested <sup>2</sup> rebuilt, not "80 m 2"
    assert s.passes_filters(h) is True


def test_olsthoorn_card_sold_and_onder_bod_skipped():
    page = _adaptor(OLSTHOORN_GRID_HTML, "https://www.olsthoornmakelaars.nl/wonen/")
    cards = page.css("a.card-house")
    assert s._parse_olsthoorn_card(cards[1]) is None  # Verkocht
    assert s._parse_olsthoorn_card(cards[2]) is None  # Onder bod


def test_olsthoorn_card_non_delft_skipped():
    page = _adaptor(OLSTHOORN_GRID_HTML, "https://www.olsthoornmakelaars.nl/wonen/")
    card = page.css("a.card-house")[3]
    assert s._parse_olsthoorn_card(card) is None  # Rijswijk


EMPTY_GRID_HTML = '<div class="section--houses"></div>'


def test_scrape_olsthoorn_sales_paginates_until_empty(monkeypatch):
    pytest.importorskip("scrapling.parser")
    fetched_urls = []

    def fake_http_get(url, timeout=30):
        fetched_urls.append(url)
        if url == s.OLSTHOORN_WONEN_URL:
            return OLSTHOORN_GRID_HTML.encode()
        return EMPTY_GRID_HTML.encode()

    monkeypatch.setattr(s, "_http_get", fake_http_get)
    houses = s.scrape_olsthoorn_sales(set())

    assert len(houses) == 1
    assert houses[0]["straatnaamHuisnummer"] == "Voorstraat 1"
    # Stops at the first empty page instead of crawling all 25.
    assert fetched_urls == [
        s.OLSTHOORN_WONEN_URL,
        s.OLSTHOORN_WONEN_PAGE_URL.format(page=2),
    ]


# ---------------------------------------------------------------------------
# Van Silfhout Makelaars koop grid (WordPress + FacetWP REST refresh)
# ---------------------------------------------------------------------------
# The bare "Kamers" number on each card is already total kamers (cross-checked
# against a real detail page's separate "Slaapkamers" row), so no
# bedrooms_to_kamers conversion is needed here. A "Prijs Op Aanvraag" listing
# uses a placeholder "€ 1 k.k." under a different label — passes_filters must
# reject it rather than treat it as a genuine sub-270k match.

VANSILFHOUT_TEMPLATE_HTML = """
<div class="objectcontainer">
  <article class="row">
    <div class="col-sm-6 col-md-6 noPdr">
      <a class="objectcontainerimg" href="https://www.vansilfhout.nl/aanbod/voorstraat-1-delft/">
        <span class="objectstatus ">Te koop</span>
      </a>
    </div>
    <div class="col-sm-6 col-md-6 objectcontainerinfo">
      <a class="straatnaamwoonplaats" href="https://www.vansilfhout.nl/aanbod/voorstraat-1-delft/">
        <h2 class="objecttitle">Voorstraat 1</h2>
        <span>Delft</span>
      </a>
      <ul class="shortSpecs">
        <li><span>Vraagprijs:</span> <strong>€ 250.000 k.k.</strong></li>
        <li><span>Oppervlakte:</span> <span>80 m<sup>2</sup></span></li>
        <li><span>Kamers:</span> <span>3</span></li>
      </ul>
      <a class="bekijkenlink" href="https://www.vansilfhout.nl/aanbod/voorstraat-1-delft/">Bekijken</a>
    </div>
  </article>
</div>
<div class="objectcontainer">
  <article class="row">
    <div class="col-sm-6 col-md-6 noPdr">
      <a class="objectcontainerimg" href="https://www.vansilfhout.nl/aanbod/achterstraat-9-delft/">
        <span class="objectstatus ">Te koop</span>
      </a>
    </div>
    <div class="col-sm-6 col-md-6 objectcontainerinfo">
      <a class="straatnaamwoonplaats" href="https://www.vansilfhout.nl/aanbod/achterstraat-9-delft/">
        <h2 class="objecttitle">Achterstraat 9</h2>
        <span>Delft</span>
      </a>
      <ul class="shortSpecs">
        <li><span>Prijs Op Aanvraag:</span> <strong>€ 1 k.k.</strong></li>
        <li><span>Oppervlakte:</span> <span>803 m<sup>2</sup></span></li>
        <li><span>Kamers:</span> <span>8</span></li>
      </ul>
      <a class="bekijkenlink" href="https://www.vansilfhout.nl/aanbod/achterstraat-9-delft/">Bekijken</a>
    </div>
  </article>
</div>
<div class="objectcontainer">
  <article class="row">
    <div class="col-sm-6 col-md-6 noPdr">
      <a class="objectcontainerimg" href="https://www.vansilfhout.nl/aanbod/kerkstraat-2-rijswijk/">
        <span class="objectstatus ">Te koop</span>
      </a>
    </div>
    <div class="col-sm-6 col-md-6 objectcontainerinfo">
      <a class="straatnaamwoonplaats" href="https://www.vansilfhout.nl/aanbod/kerkstraat-2-rijswijk/">
        <h2 class="objecttitle">Kerkstraat 2</h2>
        <span>Rijswijk</span>
      </a>
      <ul class="shortSpecs">
        <li><span>Vraagprijs:</span> <strong>€ 230.000 k.k.</strong></li>
        <li><span>Oppervlakte:</span> <span>70 m<sup>2</sup></span></li>
        <li><span>Kamers:</span> <span>3</span></li>
      </ul>
      <a class="bekijkenlink" href="https://www.vansilfhout.nl/aanbod/kerkstraat-2-rijswijk/">Bekijken</a>
    </div>
  </article>
</div>
"""


# ---------------------------------------------------------------------------
# De Bruyn en Tak koop
# ---------------------------------------------------------------------------

DEBRUYNENTAK_KOOP_HTML = """
<div class="objectList">
  <div class="item">
    <div class="label">Beschikbaar</div>
    <div class="itemContent">
      <a href="https://www.debruynentak.nl/voorstraat-1.html"
         class="itemTitel">
        <span class="objectTitel">Voorstraat 1</span>
        <span class="itemSubtitel">Delft</span>
      </a>
      <span class="itemSpecs">3 kamer appartement, 72 m²</span>
      <div class="itemPrice">
        <span class="currency">&euro;</span>
        <span class="price">265.000,-</span>
        <span class="priceSuffix">k.k.</span>
      </div>
    </div>
  </div>
  <div class="item">
    <div class="label red">Verkocht</div>
    <div class="itemContent">
      <a href="https://www.debruynentak.nl/achterstraat-9.html"
         class="itemTitel">
        <span class="objectTitel">Achterstraat 9</span>
        <span class="itemSubtitel">Delft</span>
      </a>
      <span class="itemSpecs">4 kamer appartement, 97 m²</span>
      <div class="itemPrice">
        <span class="price">319.000,-</span>
      </div>
    </div>
  </div>
  <div class="item">
    <div class="label">Beschikbaar</div>
    <div class="itemContent">
      <a href="https://www.debruynentak.nl/kerkstraat-5.html"
         class="itemTitel">
        <span class="objectTitel">Kerkstraat 5</span>
        <span class="itemSubtitel">Voorburg</span>
      </a>
      <span class="itemSpecs">5 kamer woonhuis, 120 m²</span>
      <div class="itemPrice">
        <span class="price">450.000,-</span>
      </div>
    </div>
  </div>
</div>
"""


def test_vansilfhout_card_available_delft_parsed():
    page = _adaptor(
        VANSILFHOUT_TEMPLATE_HTML, "https://www.vansilfhout.nl/woningaanbod/"
    )
    card = page.css(".objectcontainer")[0]
    h = s._parse_vansilfhout_card(card)
    assert h is not None
    assert h["url"] == "https://www.vansilfhout.nl/aanbod/voorstraat-1-delft/"
    assert h["straatnaamHuisnummer"] == "Voorstraat 1"
    assert h["plaats"] == "Delft"
    assert h["vraagprijs"] == "€ 250.000 k.k."
    assert h["oppervlakte"] == "80 m²"  # nested <sup>2</sup> rebuilt, not "80 m 2"
    assert h["kamers"] == "3 kamers"  # bare card number is already total kamers
    assert s.passes_filters(h) is True


def test_vansilfhout_card_price_on_request_excluded():
    page = _adaptor(
        VANSILFHOUT_TEMPLATE_HTML, "https://www.vansilfhout.nl/woningaanbod/"
    )
    card = page.css(".objectcontainer")[1]
    h = s._parse_vansilfhout_card(card)
    assert h is not None
    assert h["vraagprijs"] == ""  # "Prijs Op Aanvraag" isn't "Vraagprijs"
    assert s.passes_filters(h) is False


def test_vansilfhout_card_non_delft_skipped():
    page = _adaptor(
        VANSILFHOUT_TEMPLATE_HTML, "https://www.vansilfhout.nl/woningaanbod/"
    )
    card = page.css(".objectcontainer")[2]
    assert s._parse_vansilfhout_card(card) is None  # Rijswijk


def test_scrape_vansilfhout_sales_paginates_across_facetwp_pages(monkeypatch):
    pytest.importorskip("scrapling.parser")
    requested_pages = []

    def fake_refresh(paged):
        requested_pages.append(paged)
        if paged == 1:
            return {
                "template": VANSILFHOUT_TEMPLATE_HTML,
                "settings": {"pager": {"total_pages": 2}},
            }
        return {"template": "", "settings": {"pager": {"total_pages": 2}}}

    monkeypatch.setattr(s, "_facetwp_refresh", fake_refresh)
    houses = s.scrape_vansilfhout_sales(set())

    assert requested_pages == [1, 2]
    assert len(houses) == 2  # Delft koop cards on page 1; Rijswijk card dropped
    assert {h["straatnaamHuisnummer"] for h in houses} == {
        "Voorstraat 1",
        "Achterstraat 9",
    }


# ---------------------------------------------------------------------------
# De Bruyn en Tak koop
# ---------------------------------------------------------------------------


def test_debruynentak_koop_parses_available_delft(monkeypatch):
    pytest.importorskip("scrapling.parser")
    monkeypatch.setattr(
        s, "_http_get", lambda url, timeout=30: DEBRUYNENTAK_KOOP_HTML.encode()
    )
    houses = s.scrape_debruynentak_sales(set())
    assert len(houses) == 1
    h = houses[0]
    assert h["straatnaamHuisnummer"] == "Voorstraat 1"
    assert h["plaats"] == "Delft"
    assert "265.000" in h["vraagprijs"]
    assert h["kamers"] == "3 kamers"
    assert h["oppervlakte"] == "72 m²"


def test_debruynentak_koop_skips_verkocht(monkeypatch):
    pytest.importorskip("scrapling.parser")
    monkeypatch.setattr(
        s, "_http_get", lambda url, timeout=30: DEBRUYNENTAK_KOOP_HTML.encode()
    )
    houses = s.scrape_debruynentak_sales(set())
    urls = {h["url"] for h in houses}
    assert "https://www.debruynentak.nl/achterstraat-9.html" not in urls


def test_debruynentak_koop_skips_non_delft(monkeypatch):
    pytest.importorskip("scrapling.parser")
    monkeypatch.setattr(
        s, "_http_get", lambda url, timeout=30: DEBRUYNENTAK_KOOP_HTML.encode()
    )
    houses = s.scrape_debruynentak_sales(set())
    cities = {h["plaats"] for h in houses}
    assert "Voorburg" not in cities


# ---------------------------------------------------------------------------
# Van Gulden Makelaardij koop
# ---------------------------------------------------------------------------

VANGULDEN_KOOP_HTML = """
<div>
  <a href="https://vanguldenmakelaardij.nl/aanbod-detail/woningaanbod/delft/bestaande-woning/23481-voorstraat-1/">
    <div class="titel">Voorstraat 1</div>
    <p class="notranslate">Delft</p>
    <div class="price">&euro; 239.000,- k.k.</div>
    <div class="kenmerk">
      <img alt="woonoppervlakte_icon" />55 m²
    </div>
    <div class="kenmerk">
      <img alt="kamers_icon" />2
    </div>
  </a>
  <a href="https://vanguldenmakelaardij.nl/aanbod-detail/woningaanbod/rijswijk/bestaande-woning/22663-kerkstraat-5/">
    <div class="titel">Kerkstraat 5</div>
    <p class="notranslate">Rijswijk</p>
    <div class="price">&euro; 239.000,- k.k.</div>
  </a>
  <a href="https://vanguldenmakelaardij.nl/aanbod-detail/woningaanbod/delft/bestaande-woning/99999-huurhuis/">
    <div class="titel">Huurhuis 1</div>
    <p class="notranslate">Delft</p>
    <div class="price">&euro; 1.200,- per maand</div>
  </a>
</div>
"""


def test_vangulden_koop_parses_delft_koop(monkeypatch):
    pytest.importorskip("scrapling.parser")
    monkeypatch.setattr(
        s, "_http_get", lambda url, timeout=30: VANGULDEN_KOOP_HTML.encode()
    )
    houses = s.scrape_vangulden_sales(set())
    assert len(houses) == 1
    h = houses[0]
    assert h["straatnaamHuisnummer"] == "Voorstraat 1"
    assert h["plaats"] == "Delft"
    assert "239.000" in h["vraagprijs"]
    assert h["oppervlakte"] == "55 m²"
    assert h["kamers"] == "2"


def test_vangulden_koop_skips_huur(monkeypatch):
    pytest.importorskip("scrapling.parser")
    monkeypatch.setattr(
        s, "_http_get", lambda url, timeout=30: VANGULDEN_KOOP_HTML.encode()
    )
    houses = s.scrape_vangulden_sales(set())
    addrs = {h["straatnaamHuisnummer"] for h in houses}
    assert "Huurhuis 1" not in addrs


def test_vangulden_koop_skips_non_delft(monkeypatch):
    pytest.importorskip("scrapling.parser")
    monkeypatch.setattr(
        s, "_http_get", lambda url, timeout=30: VANGULDEN_KOOP_HTML.encode()
    )
    houses = s.scrape_vangulden_sales(set())
    cities = {h["plaats"] for h in houses}
    assert "Rijswijk" not in cities


# ---------------------------------------------------------------------------
# Frisia Makelaars koop (sitemap + detail pages)
# ---------------------------------------------------------------------------

FRISIA_KOOP_DETAIL = """
<html><body>
  <h1>Voorstraat 1 , 2611 AB, Delft</h1>
  <div class="panel__block__feature">Vraagprijs | &euro; 260.000,- k.k.</div>
  <div class="panel__block__feature">Status | Beschikbaar</div>
  <section class="section--intro__list">
    <ul>
      <li><i class="icon-livearea"></i>72 m²</li>
      <li><i class="icon-bedroom"></i>2</li>
    </ul>
  </section>
</body></html>
"""

FRISIA_RENT_DETAIL = """
<html><body>
  <h1>Achterstraat 9 , 2611 CD, Delft</h1>
  <div class="panel__block__feature">Huurprijs | &euro; 1.200 p.m.</div>
</body></html>
"""


def test_frisia_koop_detail_parsed():
    pytest.importorskip("scrapling.parser")
    h = s._parse_frisia_koop_listing(
        "https://frisiamakelaars.nl/wonen/aanbod/voorstraat-1-delft-x",
        FRISIA_KOOP_DETAIL.encode(),
    )
    assert h is not None
    assert h["straatnaamHuisnummer"] == "Voorstraat 1"
    assert h["plaats"] == "Delft"
    assert "260.000" in h["vraagprijs"]
    assert h["oppervlakte"] == "72 m²"


def test_frisia_koop_skips_rental():
    pytest.importorskip("scrapling.parser")
    h = s._parse_frisia_koop_listing(
        "https://frisiamakelaars.nl/wonen/aanbod/achterstraat-9-delft-y",
        FRISIA_RENT_DETAIL.encode(),
    )
    assert h is None


FRISIA_KOOP_SOLD_DETAIL = """
<html><body>
  <h1>Markt 3 , 2611 AB, Delft</h1>
  <div class="panel__block__feature">Vraagprijs | &euro; 250.000,- k.k.</div>
  <div class="panel__block__feature">Status | Verkocht onder voorbehoud</div>
</body></html>
"""


def test_frisia_koop_skips_sold():
    pytest.importorskip("scrapling.parser")
    h = s._parse_frisia_koop_listing(
        "https://frisiamakelaars.nl/wonen/aanbod/markt-3-delft-z",
        FRISIA_KOOP_SOLD_DETAIL.encode(),
    )
    assert h is None


# ---------------------------------------------------------------------------
# Marloes Makelaars koop (sitemap + detail pages)
# ---------------------------------------------------------------------------

MARLOES_KOOP_DETAIL = """
<html><head><title>Voorstraat 1 te Delft | Marloes Makelaars</title></head>
<body>
  <dl>
    <dt>Prijs</dt><dd>&euro; 265.000,- k.k.</dd>
    <dt>Plaats</dt><dd>Delft</dd>
    <dt>Oppervlakte</dt><dd>43 m²</dd>
    <dt>Slaapkamers</dt><dd>1</dd>
    <dt>Status</dt><dd>Beschikbaar</dd>
  </dl>
</body></html>
"""

MARLOES_RENT_DETAIL = """
<html><head><title>Achterstraat 9 te Delft | Marloes Makelaars</title></head>
<body>
  <dl>
    <dt>Prijs</dt><dd>&euro; 1.200,- per maand</dd>
    <dt>Plaats</dt><dd>Delft</dd>
  </dl>
</body></html>
"""


def test_marloes_koop_detail_parsed():
    pytest.importorskip("scrapling.parser")
    h = s._parse_marloes_koop_listing(
        "https://www.marloesmakelaars.nl/woning/voorstraat-1-te-delft/",
        MARLOES_KOOP_DETAIL.encode(),
    )
    assert h is not None
    assert h["straatnaamHuisnummer"] == "Voorstraat 1"
    assert h["plaats"] == "Delft"
    assert "265.000" in h["vraagprijs"]
    assert h["oppervlakte"] == "43 m²"
    assert h["kamers"] == "2 kamers"


def test_marloes_koop_skips_rental():
    pytest.importorskip("scrapling.parser")
    h = s._parse_marloes_koop_listing(
        "https://www.marloesmakelaars.nl/woning/achterstraat-9-te-delft/",
        MARLOES_RENT_DETAIL.encode(),
    )
    assert h is None


MARLOES_SOLD_DETAIL = """
<html><head><title>Markt 3 te Delft | Marloes Makelaars</title></head>
<body>
  <dl>
    <dt>Prijs</dt><dd>&euro; 250.000,- k.k.</dd>
    <dt>Plaats</dt><dd>Delft</dd>
    <dt>Status</dt><dd>Verkocht</dd>
  </dl>
</body></html>
"""


def test_marloes_koop_skips_sold():
    pytest.importorskip("scrapling.parser")
    h = s._parse_marloes_koop_listing(
        "https://www.marloesmakelaars.nl/woning/markt-3-te-delft/",
        MARLOES_SOLD_DETAIL.encode(),
    )
    assert h is None


# ---------------------------------------------------------------------------
# PSG Wonen koop (Hayweb sale sitemap — reuses Prinsenstad parser)
# ---------------------------------------------------------------------------


def test_psgwonen_koop_uses_prinsenstad_parser():
    pytest.importorskip("scrapling.parser")
    body = _prinsenstad_detail(
        "Beschikbaar", "Te koop: Landelaan 15F104, 2282 GD Rijswijk"
    )
    h = s._parse_prinsenstad_koop_listing(
        "https://www.psg-wonen.nl/woningaanbod/koop/rijswijk/landelaan/15f104",
        body,
    )
    assert h is not None
    assert h["straatnaamHuisnummer"] == "Landelaan 15F104"
    assert h["plaats"] == "Rijswijk"


PSGWONEN_SALE_SITEMAP = """<?xml version="1.0" encoding="UTF-8"?>
<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">
  <url><loc>https://www.psg-wonen.nl/woningaanbod/koop/delft/voorstraat/1</loc></url>
  <url><loc>https://www.psg-wonen.nl/woningaanbod/koop/rijswijk/kerkstraat/5</loc></url>
  <url><loc>https://www.psg-wonen.nl/woningaanbod/koop/rijswijk/type-appartement</loc></url>
</urlset>
"""


def test_psgwonen_koop_filters_delft_from_sitemap(monkeypatch):
    pytest.importorskip("scrapling.parser")
    detail_body = _prinsenstad_detail(
        "Beschikbaar", "Te koop: Voorstraat 1, 2611 AB Delft"
    )

    def fake_http_get(url, timeout=30):
        if url == s.PSGWONEN_SALE_SITEMAP_URL:
            return PSGWONEN_SALE_SITEMAP.encode()
        return detail_body

    monkeypatch.setattr(s, "_http_get", fake_http_get)
    houses = s.scrape_psgwonen_sales(set())
    assert len(houses) == 1
    assert houses[0]["straatnaamHuisnummer"] == "Voorstraat 1"
    assert houses[0]["plaats"] == "Delft"


# ---------------------------------------------------------------------------
# DB schema migration (tg_message_ids + status columns)
# ---------------------------------------------------------------------------


def test_init_db_creates_new_columns(tmp_path, monkeypatch):
    monkeypatch.setattr(s, "DB_PATH", str(tmp_path / "sales.sqlite"))
    conn = s.init_db()
    cols = {row[1] for row in conn.execute("PRAGMA table_info(sales)")}
    assert "tg_message_ids" in cols
    assert "status" in cols
    conn.close()


def test_init_db_migrates_existing_table(tmp_path, monkeypatch):
    db_path = str(tmp_path / "sales.sqlite")
    monkeypatch.setattr(s, "DB_PATH", db_path)
    conn = sqlite3.connect(db_path)
    conn.execute("""
        CREATE TABLE sales (
            url TEXT PRIMARY KEY,
            straatnaamHuisnummer TEXT,
            plaats TEXT,
            vraagprijs TEXT,
            oppervlakte TEXT,
            kamers TEXT
        )
    """)
    conn.execute(
        "INSERT INTO sales VALUES (?, ?, ?, ?, ?, ?)",
        ("https://a.nl/1", "Voorstraat 1", "Delft", "€ 250.000", "80 m²", "3 kamers"),
    )
    conn.commit()
    conn.close()

    conn = s.init_db()
    cols = {row[1] for row in conn.execute("PRAGMA table_info(sales)")}
    assert "tg_message_ids" in cols
    assert "status" in cols
    row = conn.execute("SELECT tg_message_ids, status FROM sales").fetchone()
    assert row == ("", "available")
    conn.close()


# ---------------------------------------------------------------------------
# Telegram message ID storage and deletion
# ---------------------------------------------------------------------------


def test_send_returns_message_ids(monkeypatch):
    monkeypatch.setattr(s, "TELEGRAM_BOT_TOKEN", "fake-token")

    def fake_urlopen(req, timeout=10):
        class FakeResp:
            def read(self):
                return json.dumps(
                    {"ok": True, "result": {"message_id": 42}}
                ).encode()
            def __enter__(self):
                return self
            def __exit__(self, *a):
                pass
        return FakeResp()

    monkeypatch.setattr(s.urllib.request, "urlopen", fake_urlopen)
    result = s._send("-100123", "hello")
    assert len(result) == 1
    assert result[0]["chat_id"] == "-100123"
    assert result[0]["message_id"] == 42


def test_send_returns_empty_on_failure(monkeypatch):
    monkeypatch.setattr(s, "TELEGRAM_BOT_TOKEN", "fake-token")

    def fake_urlopen(req, timeout=10):
        raise Exception("network error")

    monkeypatch.setattr(s.urllib.request, "urlopen", fake_urlopen)
    result = s._send("-100123", "hello")
    assert result == []


def test_process_houses_stores_message_ids(db, monkeypatch):
    sent_ids = [{"chat_id": "-100", "message_id": 99}]
    monkeypatch.setattr(s, "notify_new_listing", lambda h: sent_ids)
    h = _house(url="https://a.nl/1")
    existing = s.get_existing_urls(db)
    s.process_houses(db, [h], existing, seeding=False)

    row = db.execute(
        "SELECT tg_message_ids FROM sales WHERE url = ?", ("https://a.nl/1",)
    ).fetchone()
    assert row is not None
    assert json.loads(row[0]) == sent_ids


def test_process_houses_seeding_no_message_ids(db, monkeypatch):
    monkeypatch.setattr(s, "notify_new_listing", lambda h: [])
    h = _house(url="https://a.nl/1")
    existing = s.get_existing_urls(db)
    s.process_houses(db, [h], existing, seeding=True)

    row = db.execute(
        "SELECT tg_message_ids, status FROM sales WHERE url = ?", ("https://a.nl/1",)
    ).fetchone()
    assert row[0] == ""
    assert row[1] == "available"


# ---------------------------------------------------------------------------
# Sold URL detection via _cycle_sold_urls
# ---------------------------------------------------------------------------


def test_json_feed_records_sold_urls(monkeypatch):
    s._cycle_sold_urls.clear()
    entries = [
        _feed_entry(statusOrig="sold", url="/nl/aanbod/koop/delft/sold-flat/abc"),
        _feed_entry(statusOrig="under_bid", url="/nl/aanbod/koop/delft/bid-flat/def"),
        _feed_entry(statusOrig="available", url="/nl/aanbod/koop/delft/ok-flat/ghi"),
    ]
    monkeypatch.setattr(
        s, "_http_get", lambda url, timeout=30: json.dumps(entries).encode()
    )
    houses = s._scrape_realtime_listings_sales(
        "https://feed", "https://vandaal.nl", "Van Daal"
    )
    assert len(houses) == 1
    assert "https://vandaal.nl/nl/aanbod/koop/delft/sold-flat/abc" in s._cycle_sold_urls
    assert "https://vandaal.nl/nl/aanbod/koop/delft/bid-flat/def" in s._cycle_sold_urls
    assert (
        "https://vandaal.nl/nl/aanbod/koop/delft/ok-flat/ghi"
        not in s._cycle_sold_urls
    )


def test_realworks_records_verkocht_as_koop_url():
    pytest.importorskip("scrapling.parser")
    s._cycle_sold_urls.clear()

    html = """
    <ul>
      <li class="aanbodEntry">
        <a class="aanbodEntryLink"
           href="/aanbod/woningaanbod/verkocht/voorstraat-1/">x</a>
        <h3 class="street-address">Voorstraat 1</h3>
        <span class="locality">Delft</span>
        <span class="kenmerkValue">€ 260.000 k.k.</span>
      </li>
    </ul>
    """
    page = _adaptor(html, "https://www.zomakelaars.nl/aanbod")
    houses = s._scrape_realworks_koop(
        page, "https://www.zomakelaars.nl", "ZO"
    )
    assert len(houses) == 0
    expected = "https://www.zomakelaars.nl/aanbod/woningaanbod/koop/voorstraat-1/"
    assert expected in s._cycle_sold_urls


def test_olsthoorn_records_sold_urls():
    pytest.importorskip("scrapling.parser")
    s._cycle_sold_urls.clear()

    page = _adaptor(OLSTHOORN_GRID_HTML, "https://www.olsthoornmakelaars.nl/wonen/")
    cards = page.css("a.card-house")
    s._parse_olsthoorn_card(cards[1])  # Verkocht
    s._parse_olsthoorn_card(cards[2])  # Onder bod

    assert (
        "https://www.olsthoornmakelaars.nl/wonen/object/achterstraat-9-delft/"
        in s._cycle_sold_urls
    )
    assert (
        "https://www.olsthoornmakelaars.nl/wonen/object/dorpsstraat-3-onder-bod/"
        in s._cycle_sold_urls
    )


def test_debruynentak_records_verkocht_url(monkeypatch):
    pytest.importorskip("scrapling.parser")
    s._cycle_sold_urls.clear()
    monkeypatch.setattr(
        s, "_http_get", lambda url, timeout=30: DEBRUYNENTAK_KOOP_HTML.encode()
    )
    s.scrape_debruynentak_sales(set())
    assert (
        "https://www.debruynentak.nl/achterstraat-9.html" in s._cycle_sold_urls
    )


# ---------------------------------------------------------------------------
# _delete_listing_messages + process_sold_urls
# ---------------------------------------------------------------------------


def test_delete_listing_messages_marks_sold(db, monkeypatch):
    msg_ids = [{"chat_id": "-100", "message_id": 42}]
    db.execute(
        "INSERT INTO sales (url, straatnaamHuisnummer, plaats, vraagprijs, "
        "oppervlakte, kamers, tg_message_ids, status) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        (
            "https://a.nl/1", "Voorstraat 1", "Delft",
            "€ 250.000", "80 m²", "3 kamers",
            json.dumps(msg_ids), "available",
        ),
    )
    db.commit()

    deleted_calls = []
    monkeypatch.setattr(
        s, "_delete_message",
        lambda cid, mid: (deleted_calls.append((cid, mid)), True)[1],
    )

    result = s._delete_listing_messages(db, "https://a.nl/1")
    assert result == "Voorstraat 1"
    assert deleted_calls == [("-100", 42)]

    row = db.execute(
        "SELECT status FROM sales WHERE url = ?", ("https://a.nl/1",)
    ).fetchone()
    assert row[0] == "sold"


def test_delete_listing_messages_skips_already_sold(db, monkeypatch):
    db.execute(
        "INSERT INTO sales (url, straatnaamHuisnummer, plaats, vraagprijs, "
        "oppervlakte, kamers, tg_message_ids, status) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        (
            "https://a.nl/1", "Voorstraat 1", "Delft",
            "€ 250.000", "80 m²", "3 kamers", "[]", "sold",
        ),
    )
    db.commit()
    monkeypatch.setattr(s, "_delete_message", lambda *a: True)
    assert s._delete_listing_messages(db, "https://a.nl/1") is None


def test_delete_listing_messages_no_tg_ids_still_marks_sold(db, monkeypatch):
    db.execute(
        "INSERT INTO sales (url, straatnaamHuisnummer, plaats, vraagprijs, "
        "oppervlakte, kamers, tg_message_ids, status) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        (
            "https://a.nl/1", "Voorstraat 1", "Delft",
            "€ 250.000", "80 m²", "3 kamers", "", "available",
        ),
    )
    db.commit()

    result = s._delete_listing_messages(db, "https://a.nl/1")
    assert result == "Voorstraat 1"

    row = db.execute(
        "SELECT status FROM sales WHERE url = ?", ("https://a.nl/1",)
    ).fetchone()
    assert row[0] == "sold"


def test_process_sold_urls_returns_addresses(db, monkeypatch):
    msg_ids = [{"chat_id": "-100", "message_id": 55}]
    db.execute(
        "INSERT INTO sales (url, straatnaamHuisnummer, plaats, vraagprijs, "
        "oppervlakte, kamers, tg_message_ids, status) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        (
            "https://a.nl/1", "Voorstraat 1", "Delft",
            "€ 250.000", "80 m²", "3 kamers",
            json.dumps(msg_ids), "available",
        ),
    )
    db.commit()

    deleted_calls = []
    monkeypatch.setattr(
        s, "_delete_message",
        lambda cid, mid: (deleted_calls.append((cid, mid)), True)[1],
    )

    removed = s.process_sold_urls(
        db, {"https://a.nl/1", "https://unknown.nl/2"}
    )
    assert removed == ["Voorstraat 1"]
    assert deleted_calls == [("-100", 55)]


def test_process_sold_urls_ignores_unknown_urls(db, monkeypatch):
    monkeypatch.setattr(s, "_delete_message", lambda *a: True)
    removed = s.process_sold_urls(db, {"https://unknown.nl/1"})
    assert removed == []


# ---------------------------------------------------------------------------
# recheck_available_listings (universal fallback)
# ---------------------------------------------------------------------------


def test_recheck_detects_sold_listing(db, monkeypatch):
    msg_ids = [{"chat_id": "-100", "message_id": 77}]
    db.execute(
        "INSERT INTO sales (url, straatnaamHuisnummer, plaats, vraagprijs, "
        "oppervlakte, kamers, tg_message_ids, status) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        (
            "https://a.nl/1", "Voorstraat 1", "Delft",
            "€ 250.000", "80 m²", "3 kamers",
            json.dumps(msg_ids), "available",
        ),
    )
    db.commit()

    monkeypatch.setattr(
        s, "_http_get",
        lambda url, timeout=15: b"<html>Verkocht onder voorbehoud</html>",
    )
    deleted_calls = []
    monkeypatch.setattr(
        s, "_delete_message",
        lambda cid, mid: (deleted_calls.append((cid, mid)), True)[1],
    )

    removed = s.recheck_available_listings(db)
    assert removed == ["Voorstraat 1"]
    assert deleted_calls == [("-100", 77)]

    row = db.execute(
        "SELECT status FROM sales WHERE url = ?", ("https://a.nl/1",)
    ).fetchone()
    assert row[0] == "sold"


def test_recheck_keeps_available_listing(db, monkeypatch):
    db.execute(
        "INSERT INTO sales (url, straatnaamHuisnummer, plaats, vraagprijs, "
        "oppervlakte, kamers, tg_message_ids, status) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        (
            "https://a.nl/1", "Voorstraat 1", "Delft",
            "€ 250.000", "80 m²", "3 kamers", "[]", "available",
        ),
    )
    db.commit()

    monkeypatch.setattr(
        s, "_http_get",
        lambda url, timeout=15: b"<html>Te koop Beschikbaar</html>",
    )

    removed = s.recheck_available_listings(db)
    assert removed == []

    row = db.execute(
        "SELECT status FROM sales WHERE url = ?", ("https://a.nl/1",)
    ).fetchone()
    assert row[0] == "available"


def test_recheck_skips_on_fetch_failure(db, monkeypatch):
    db.execute(
        "INSERT INTO sales (url, straatnaamHuisnummer, plaats, vraagprijs, "
        "oppervlakte, kamers, tg_message_ids, status) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        (
            "https://a.nl/1", "Voorstraat 1", "Delft",
            "€ 250.000", "80 m²", "3 kamers", "[]", "available",
        ),
    )
    db.commit()

    def failing_get(url, timeout=15):
        raise Exception("network error")

    monkeypatch.setattr(s, "_http_get", failing_get)
    removed = s.recheck_available_listings(db)
    assert removed == []

    row = db.execute(
        "SELECT status FROM sales WHERE url = ?", ("https://a.nl/1",)
    ).fetchone()
    assert row[0] == "available"


# ---------------------------------------------------------------------------
# Prinsenstad / Frisia / Marloes sold detection via detail page re-parse
# ---------------------------------------------------------------------------


def test_prinsenstad_sold_records_url():
    pytest.importorskip("scrapling.parser")
    s._cycle_sold_urls.clear()
    body = _prinsenstad_detail("Verkocht", "Verkocht: Voorstraat 1, 2611 AB Delft")
    url = "https://prinsenstadmakelaardij.nl/woningaanbod/koop/delft/voorstraat/1"
    s._parse_prinsenstad_koop_listing(url, body)
    assert url in s._cycle_sold_urls


def test_frisia_sold_records_url():
    pytest.importorskip("scrapling.parser")
    s._cycle_sold_urls.clear()
    url = "https://frisiamakelaars.nl/wonen/aanbod/markt-3-delft-z"
    s._parse_frisia_koop_listing(url, FRISIA_KOOP_SOLD_DETAIL.encode())
    assert url in s._cycle_sold_urls


def test_marloes_sold_records_url():
    pytest.importorskip("scrapling.parser")
    s._cycle_sold_urls.clear()
    url = "https://www.marloesmakelaars.nl/woning/markt-3-te-delft/"
    s._parse_marloes_koop_listing(url, MARLOES_SOLD_DETAIL.encode())
    assert url in s._cycle_sold_urls


# ---------------------------------------------------------------------------
# Sold summary message (sent to Telegram, replaces previous summary)
# ---------------------------------------------------------------------------


def test_send_sold_summary_sends_and_stores_ids(monkeypatch):
    s._last_sold_summary_ids = []
    sent_texts = []
    monkeypatch.setattr(
        s, "_send",
        lambda chat_ids, text: (
            sent_texts.append(text),
            [{"chat_id": "-100", "message_id": 200}],
        )[1],
    )
    monkeypatch.setattr(s, "_delete_message", lambda *a: True)

    s._send_sold_summary(["Voorstraat 1", "Achterstraat 9"])

    assert len(sent_texts) == 1
    assert "Voorstraat 1" in sent_texts[0]
    assert "Achterstraat 9" in sent_texts[0]
    assert "2 woningen" in sent_texts[0]
    assert s._last_sold_summary_ids == [{"chat_id": "-100", "message_id": 200}]


def test_send_sold_summary_deletes_previous(monkeypatch):
    s._last_sold_summary_ids = [{"chat_id": "-100", "message_id": 150}]
    deleted_calls = []
    monkeypatch.setattr(
        s, "_delete_message",
        lambda cid, mid: (deleted_calls.append((cid, mid)), True)[1],
    )
    monkeypatch.setattr(
        s, "_send",
        lambda chat_ids, text: [{"chat_id": "-100", "message_id": 201}],
    )

    s._send_sold_summary(["Markt 3"])

    assert ("-100", 150) in deleted_calls
    assert s._last_sold_summary_ids == [{"chat_id": "-100", "message_id": 201}]


def test_send_sold_summary_singular(monkeypatch):
    s._last_sold_summary_ids = []
    sent_texts = []
    monkeypatch.setattr(
        s, "_send",
        lambda chat_ids, text: (sent_texts.append(text), [])[1],
    )
    monkeypatch.setattr(s, "_delete_message", lambda *a: True)

    s._send_sold_summary(["Voorstraat 1"])
    assert "1 woning" in sent_texts[0]
