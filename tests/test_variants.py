"""Warianty wydarzenia (sesja × grupa) ze sklepu WooCommerce, godzina / nitka toru i widok „dzień -> wybory”."""

import html
import json
from datetime import date

from tests.test_agent import ev
from trackday.agents import event_scout
from trackday.agents.event_scout import EventScoutAgent, ScoutInput, _parse_time
from trackday.config import Settings
from trackday.llm import FakeLLM
from trackday.models import EventVariant, ExtractionResult, TrackDayEvent
from trackday.sources import make_source
from trackday.tools.fetch import FetchResult
from trackday.tools.variants import extract_variants
from trackday.views import all_groups, choice_rows, group_by_day


def variation(session, level, stock_html, in_stock=True, price=450):
    return {"attributes": {"attribute_pa_godziny": session, "attribute_pa_poziom": level},
            "availability_html": stock_html, "display_price": price, "is_in_stock": in_stock,
            "is_purchasable": True, "variation_is_active": True, "variation_is_visible": True, "max_qty": 1}


VARIATIONS = [
    variation("popoludnie", "zielona", '<p class="stock in-stock">3 w magazynie</p>'),
    variation("popoludnie", "zolta", '<p class="stock in-stock">1 w magazynie</p>'),
    variation("popoludnie", "czerwona", '<p class="stock out-of-stock">Brak w magazynie</p>', in_stock=False),
]


def product_page(variations=VARIATIONS, title="PIT BIKE REVERS godz. 13.30; 21.11.26"):
    data = "false" if variations is None else html.escape(json.dumps(variations), quote=True)
    return f"""<html><body><h1>{title}</h1><p>Nitka REVERS - więcej prawych zakrętów! Cena 450 zł</p>
    <form class="variations_form cart" data-product_variations="{data}">
      <label for="godz">Godziny wydarzenia</label>
      <select id="godz" name="attribute_pa_godziny"><option value="">Wybierz</option>
        <option value="popoludnie">Sesja popołudniowa</option></select>
      <label for="poz">Poziom zaawansowania</label>
      <select id="poz" name="attribute_pa_poziom"><option value="">Wybierz</option>
        <option value="zielona">Zielona - początkujący</option><option value="zolta">Żółta - średni</option>
        <option value="czerwona">Czerwona - zaawansowani</option></select>
    </form></body></html>"""


def test_extract_variants_reads_all_choices_with_seats():
    res = extract_variants(product_page())
    assert [(v.session, v.group, v.seats_left) for v in res.variants] == [
        ("Sesja popołudniowa", "Zielona - początkujący", 3),
        ("Sesja popołudniowa", "Żółta - średni", 1),
        ("Sesja popołudniowa", "Czerwona - zaawansowani", 0),
    ]
    assert res.variants[0].options == {"Godziny wydarzenia": "Sesja popołudniowa",
                                       "Poziom zaawansowania": "Zielona - początkujący"}
    assert res.variants[0].price == 450 and res.variants[0].currency == "PLN"


def test_extract_variants_ajax_and_plain_pages():
    assert extract_variants("<html><p>bez sklepu</p></html>").variants == []
    res = extract_variants(product_page(variations=None))  # warianty wczytywane przez JavaScript
    assert res.variants == [] and "JavaScript" in res.warnings[0]


def test_parse_time():
    assert _parse_time("13.30") == "13:30" and _parse_time("9:05") == "09:05"
    assert _parse_time("PIT BIKE godz. 17.30; 21.11.26", require_hint=True) == "17:30"
    assert _parse_time("Torowanie 21.11.26", require_hint=True) is None  # data to nie godzina


def test_agent_attaches_variants(monkeypatch):
    listing = '<a href="/pit-bike-revers-godz-13-30-21-11-26/">x</a>'
    monkeypatch.setattr(event_scout, "fetch_source", lambda src, s: FetchResult(listing, src.url, "text/html"))
    monkeypatch.setattr(event_scout, "fetch_url",
                        lambda url, s, robots_cache=None: FetchResult(product_page(), url, "text/html"))

    def responder(system, user):  # model podaje tylko podstawowe dane; groups z treści bywają niepełne
        return ExtractionResult(events=[ev(name="PIT BIKE REVERS godz. 13.30; 21.11.26", date_start="2026-11-21",
                                           track="Gazuu Racing Park", price=None, groups=["Pit Bike"])],
                                warnings=[], suspicious_instructions=False)

    src = make_source("https://jc.example/oferta/", organizer="JC", detail_link_pattern=r"\d{2}-\d{2}-\d{2}/?$")
    report = EventScoutAgent(FakeLLM(responder), settings=Settings(api_key=None, crawl_delay_s=0)).run(
        ScoutInput(sources=[src], reference_date=date(2026, 11, 16)))
    e = report.events[0]
    assert len(e.variants) == 3
    assert e.seats_left == 4  # 3 + 1 + 0
    assert e.groups == ["Zielona - początkujący", "Żółta - średni", "Czerwona - zaawansowani"]
    assert e.price == 450 and e.currency == "PLN"  # cena ze sklepu, mimo że model jej nie podał
    assert e.start_time == "13:30" and e.track_layout == "REVERS"  # z nazwy, gdy model nie wypełnił pól
    assert "brak informacji o miejscach" not in e.issues


def event(name, time, variants=(), groups=(), seats=None, track="Gazuu Racing Park", day=date(2026, 11, 21)):
    return TrackDayEvent(id=name, name=name, date_start=day, track=track, organizer="JC", url="https://jc.example/x",
                         source_id="s", start_time=time, variants=list(variants), groups=list(groups),
                         seats_left=seats, price=450, currency="PLN", price_pln=450)


V = [EventVariant(session="Popołudnie", group="Zielona", seats_left=3, price=450, currency="PLN"),
     EventVariant(session="Popołudnie", group="Czerwona", seats_left=0, price=450, currency="PLN")]


def test_one_row_per_day_with_choices():
    events = [event("PIT BIKE godz. 17.30", "17:30", V), event("PIT BIKE REVERS godz. 13.30", "13:30", V),
              event("Inny tor", None, groups=["A"], seats=5, track="Tor Poznań")]
    days = group_by_day(events)
    assert [(d.track, len(d.events), len(d.rows)) for d in days] == [("Gazuu Racing Park", 2, 4), ("Tor Poznań", 1, 1)]
    assert [r["Godzina"] for r in days[0].rows] == ["13:30", "13:30", "17:30", "17:30"]  # po godzinie
    assert days[0].seats_left == 6 and "sob 21.11.2026" in days[0].label()


def test_group_filter_and_only_available():
    events = [event("A", "13:30", V), event("B", None, groups=["Zielona"], seats=2, track="Tor Poznań")]
    assert all_groups(events) == ["Czerwona", "Zielona"]
    rows = choice_rows(events[0], {"Czerwona"})
    assert [r["Grupa"] for r in rows] == ["Czerwona"]
    assert choice_rows(events[0], {"Czerwona"}, only_available=True) == []  # jedyny wybór jest wyprzedany
    days = group_by_day(events, {"Zielona"})
    assert [len(d.rows) for d in days] == [1, 1]
    assert group_by_day(events, {"Niebieska"}) == []
