"""Terminy dodawane ręcznie: zrzuty ekranu (np. kalendarz z Facebooka) i wklejony opis wydarzenia."""

import io
from datetime import date

import pytest
from PIL import Image

from tests.test_agent import ev
from trackday.agents.event_scout import EventScoutAgent, ScoutInput
from trackday.config import Settings
from trackday.llm import FakeLLM, supports_vision
from trackday.models import ExtractionResult
from trackday.sources import is_facebook_url, make_source
from trackday.storage.db import EventRepository
from trackday.tools import images as images_mod
from trackday.tools.images import ImageError, prepare_image

FB = make_source("https://www.facebook.com/pzdamanetaracing/events", name="PM Racing Team", organizer="PM")


def png(w=3000, h=1500) -> bytes:
    buf = io.BytesIO()
    Image.new("RGB", (w, h), (10, 10, 10)).save(buf, format="PNG")
    return buf.getvalue()


def test_prepare_image_resizes_and_validates(monkeypatch):
    im = prepare_image(png())
    assert (im.width, im.height, im.media_type) == (2000, 1000, "image/png")
    assert Image.open(io.BytesIO(im.data)).size == (2000, 1000)
    for bad in [b"", b"<script>alert(1)</script>", b"\x89PNG\r\n\x1a\nuszkodzony"]:
        with pytest.raises(ImageError):
            prepare_image(bad, "x.png")
    monkeypatch.setattr(images_mod, "MAX_PIXELS", 1000)
    with pytest.raises(ImageError, match="za duży"):
        prepare_image(png(100, 100))


@pytest.mark.parametrize("url,expected", [
    ("https://www.facebook.com/pzdamanetaracing/events", True),
    ("https://facebook.com/profile.php?id=1&sk=events", True),
    ("https://m.facebook.com/x", True),
    ("https://fb.me/abc", True),
    ("https://notfacebook.com/x", False),
    ("https://jcgroup.com.pl/oferta/", False),
])
def test_is_facebook_url(url, expected):
    assert is_facebook_url(url) is expected


def test_claude_models_read_images():
    assert supports_vision("claude-haiku-5-5")


def calendar_responder(system, user):
    return ExtractionResult(events=[
        ev(name="Tor Poznań", date_start="2027-04-18", track="", url=None),
        ev(name="PIT BIKE", date_start="2027-06-13", track="Silesia", start_time="9:00", url=None),
        ev(name="PIT BIKE REVERS", date_start="2027-06-13", track="Silesia", track_layout="REVERS",
           start_time="13:30", url="https://evil.example/phish"),
    ], warnings=[], suspicious_instructions=False)


def test_import_manual_forces_track_and_keeps_sessions_apart():
    repo = EventRepository(":memory:")
    llm = FakeLLM(calendar_responder)
    shot = prepare_image(png(800, 800))
    rep = EventScoutAgent(llm, settings=Settings(api_key=None), repository=repo).import_manual(
        FB, images=[shot], track="Tor Poznań", reference_date=date(2026, 11, 16))

    assert llm.images == [[shot]] and "Tor: Tor Poznań" in llm.calls[0][1]
    assert len(rep.events) == 3 and len({e.id for e in rep.events}) == 3  # dwie sesje 13.06 osobno
    for e in rep.events:
        assert (e.track, e.track_known, e.source_id) == ("Tor Poznań", True, "manual:" + FB.id)
        assert e.url == FB.url  # link spoza Facebooka zastąpiony stroną organizatora
        assert "dodane ręcznie przez użytkownika" in e.issues
    assert repo.list_events() == []  # zapis dopiero po akceptacji w UI


def test_import_manual_needs_material():
    rep = EventScoutAgent(FakeLLM(calendar_responder), settings=Settings(api_key=None)).import_manual(FB)
    assert not rep.sources[0].ok and not rep.events


def test_time_from_model_notes():
    def responder(system, user):
        return ExtractionResult(events=[ev(name="Track day", date_start="2027-04-24", track="Tor Poznań",
                                           notes="Start godz. 8:30")], warnings=[], suspicious_instructions=False)

    rep = EventScoutAgent(FakeLLM(responder), settings=Settings(api_key=None)).import_manual(
        FB, text="Sobota 24.04.2027 Tor Poznań, start godz. 8:30", reference_date=date(2026, 11, 16))
    assert rep.events[0].start_time == "08:30"


def test_agent_run_skips_facebook_and_keeps_manual_events():
    repo = EventRepository(":memory:")
    agent = EventScoutAgent(FakeLLM(calendar_responder), settings=Settings(api_key=None), repository=repo)
    manual = agent.import_manual(FB, text="kalendarz", track="Tor Poznań", reference_date=date(2026, 11, 16))
    repo.upsert_events(manual.events)

    calls_before = len(agent.llm.calls)
    report = agent.run(ScoutInput(sources=[FB], reference_date=date(2026, 11, 16)))
    sr = report.sources[0]
    assert len(agent.llm.calls) == calls_before  # Facebook nie jest pobierany ani wysyłany do modelu
    assert sr.ok and "Dodaj terminy" in sr.warnings[0]
    assert len(repo.list_events()) == 3  # udany przebieg nie usuwa terminów dodanych ręcznie
