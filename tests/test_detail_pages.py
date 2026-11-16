"""Tryb „lista -> podstrony”: agent wchodzi w podstrony wydarzeń wskazane wzorcem linków (bez sieci)."""

import re
from datetime import date

import pytest

from trackday.agents import event_scout
from trackday.agents.event_scout import EventScoutAgent, ScoutInput
from trackday.config import Settings
from trackday.llm import FakeLLM
from trackday.models import ExtractionResult
from trackday.sources import SourceError, make_source
from trackday.tools.fetch import FetchError, FetchResult
from trackday.tools.html_clean import extract_links

from tests.test_agent import ev

LISTING_URL = "https://organizator.example/oferta/"
LISTING = """
<html><body>
<nav><a href="/kontakt/">Kontakt</a></nav>
<a href="/voucher-tor-poznan/">Voucher</a>
<a href="/trening-godz-13-30-21-11-26/">Gazuu 21 listopad 2026</a>
<a href="https://organizator.example/trening-godz-17-30-21-11-26/#opis">Gazuu 21 listopad 2026</a>
<a href="/trening-godz-17-30-21-11-26/">(ten sam link drugi raz)</a>
<a href="/trening-godz-13-30-28-11-26/">Gazuu 28 listopad 2026 (podstrona nie działa)</a>
<a href="https://evil.example/trening-1-1-27/">Obca domena</a>
<div style="display:none"><a href="/ukryty-1-12-26/">ukryty link</a></div>
</body></html>
"""
DETAIL = "<html><body><h1>Trening {hour}; 21.11.26</h1><p>Tor Poznań, cena 450 zł</p></body></html>"
PATTERN = r"\d{1,2}-\d{1,2}-\d{2}/?$"


def test_extract_links_visible_absolute_unique():
    links = extract_links(LISTING, LISTING_URL)
    assert "https://organizator.example/trening-godz-17-30-21-11-26/" in links
    assert links.count("https://organizator.example/trening-godz-17-30-21-11-26/") == 1  # bez #opis i powtórek
    assert not any("ukryty" in u for u in links)  # link z ukrytego elementu pominięty
    assert "https://organizator.example/kontakt/" in links


@pytest.fixture
def web(monkeypatch):
    """Podmienia pobieranie stron: strona listy + podstrony; zapisuje, które adresy pobrano."""
    fetched: list[str] = []

    def fake_fetch_source(src, settings):
        return FetchResult(LISTING, src.url, "text/html")

    def fake_fetch_url(url, settings, robots_cache=None):
        fetched.append(url)
        if "28-11-26" in url:
            raise FetchError("Serwer zwrócił kod 404.")
        hour = "13.30" if "13-30" in url else "17.30"
        return FetchResult(DETAIL.format(hour=hour), url, "text/html")

    monkeypatch.setattr(event_scout, "fetch_source", fake_fetch_source)
    monkeypatch.setattr(event_scout, "fetch_url", fake_fetch_url)
    return fetched


def responder(system: str, user: str) -> ExtractionResult:
    hour = re.search(r"Trening (\d\d\.\d\d)", user)
    if not hour:  # strona listy - model widzi tylko listę
        return ExtractionResult(events=[ev(
            name="z listy", date_start="2026-11-21", track="Tor Poznań", url=None, confidence=0.9)], warnings=[], suspicious_instructions=False)
    return ExtractionResult(events=[ev(
        name=f"Trening {hour.group(1)}", date_start="2026-11-21", track="Tor Poznań", organizer="Organizator",
        price=450, currency="PLN", url="https://www.addtoany.com/add_to/facebook", seats_left=5, confidence=0.9)], warnings=[], suspicious_instructions=False)


def run(source):
    llm = FakeLLM(responder)
    agent = EventScoutAgent(llm, settings=Settings(api_key=None, crawl_delay_s=0))
    return agent.run(ScoutInput(sources=[source], reference_date=date(2026, 11, 16))), llm


def test_detail_pages_are_followed(web):
    src = make_source(LISTING_URL, organizer="Organizator", detail_link_pattern=PATTERN)
    report, llm = run(src)

    # tylko podstrony z tej samej domeny, pasujące do wzorca, widoczne; strona listy nie trafia do modelu
    assert web == ["https://organizator.example/trening-godz-13-30-21-11-26/",
                   "https://organizator.example/trening-godz-17-30-21-11-26/",
                   "https://organizator.example/trening-godz-13-30-28-11-26/"]
    assert len(llm.calls) == 2
    sr = report.sources[0]
    assert sr.detail_pages == 2 and any("404" in w for w in sr.warnings)

    # dwie sesje tego samego dnia na tym samym torze to dwa osobne wydarzenia, każde z linkiem do swojej podstrony
    assert report.duplicates_removed == 0
    assert sorted(e.url for e in report.events) == web[:2]
    assert len({e.id for e in report.events}) == 2
    # link „udostępnij” podany przez model jest ignorowany - linkiem jest podstrona, bez uwag o linku
    assert all(not any("link" in i for i in e.issues) for e in report.events)


def test_detail_page_limit(web):
    src = make_source(LISTING_URL, detail_link_pattern=PATTERN, max_detail_pages=1)
    report, _ = run(src)
    assert len(web) == 1
    assert any("limit" in w for w in report.sources[0].warnings)


def test_no_matching_links_falls_back_to_listing(web):
    src = make_source(LISTING_URL, detail_link_pattern="nie-ma-takiego")
    report, llm = run(src)
    assert web == [] and len(llm.calls) == 1
    assert report.events[0].name == "z listy"
    assert any("Żaden link nie pasuje" in w for w in report.sources[0].warnings)


def test_plain_text_pattern_and_invalid_regex(web):
    # „trening-godz-13” to zwykły fragment adresu; „[” to niepoprawne wyrażenie -> szukane dosłownie
    src = make_source(LISTING_URL, detail_link_pattern="trening-godz-13")
    run(src)
    assert len(web) == 2
    web.clear()
    run(make_source(LISTING_URL, detail_link_pattern="["))
    assert web == []


def test_make_source_detail_validation():
    assert make_source(LISTING_URL, detail_link_pattern="  ").detail_link_pattern is None
    with pytest.raises(SourceError):
        make_source("sample://mototor_events.html", detail_link_pattern="x")
    with pytest.raises(SourceError):
        make_source(LISTING_URL, detail_link_pattern="x", max_detail_pages=500)
