"""Lista torów edytowana przez użytkownika: współrzędne, zapis w bazie, ponowne dopasowanie terminów."""

from datetime import date, datetime

import pytest

from tests.test_agent import ev
from trackday.agents.event_scout import EventScoutAgent, ScoutInput
from trackday.config import Settings
from trackday.llm import FakeLLM
from trackday.models import ExtractionResult, ScoutReport, TrackDayEvent, TrackDef
from trackday.storage.db import EventRepository
from trackday.tools.normalize import build_tracks, default_track_defs, match_track, parse_coords

GAZUU = TrackDef(name="Gazuu Racing Park", country="PL", lat=52.1, lon=21.0, aliases=["Gazuu", "hala Gazuu"])


@pytest.mark.parametrize("text,expected", [
    ("52.4176, 16.8089", (52.4176, 16.8089)),     # format z Map Google
    ("52,4176; 16,8089", (52.4176, 16.8089)),     # przecinki dziesiętne
    ("52.4176 16.8089", (52.4176, 16.8089)),
    ("(-33.5, 151.25)", (-33.5, 151.25)),
])
def test_parse_coords(text, expected):
    assert parse_coords(text) == expected


@pytest.mark.parametrize("text", ["", "52.4", "abc, def", "91, 10", "50, 181"])
def test_parse_coords_errors(text):
    with pytest.raises(ValueError):
        parse_coords(text)


def test_custom_tracks_match_by_name_and_alias():
    tracks = build_tracks([GAZUU])
    assert match_track("GAZUU RACING PARK", tracks).lat == 52.1
    assert match_track("trening w hali Gazuu", tracks).name == "Gazuu Racing Park"
    assert match_track("Tor Poznań", tracks) is None  # tylko tory z podanej listy
    assert match_track("Tor Poznań").name == "Tor Poznań"  # domyślnie - lista z pliku


def test_tracks_saved_in_db_and_seeded_once():
    repo = EventRepository(":memory:")
    repo.init_tracks(default_track_defs())
    assert len(repo.list_tracks()) == len(default_track_defs())
    repo.save_tracks([GAZUU])
    repo.init_tracks(default_track_defs())  # nie nadpisuje listy użytkownika
    assert repo.list_tracks() == [GAZUU]


def event(track):
    return TrackDayEvent(id=track, name="x", date_start=date(2027, 5, 1), track=track, organizer="X",
                         url="https://x.example/", source_id="s", issues=["tor spoza listy znanych torów"])


def test_relink_existing_events_after_adding_track():
    repo = EventRepository(":memory:")
    repo.save_run(ScoutReport(run_id="r", started_at=datetime.now(), model="m", reference_date=date(2027, 1, 1),
                              events=[event("GAZUU"), event("Tor Kielce")]))
    assert repo.relink_tracks(build_tracks([GAZUU])) == 1
    by_id = {e.id: e for e in repo.list_events()}
    g = by_id["GAZUU"]
    assert (g.track, g.track_known, g.lat, g.lon, g.issues) == ("Gazuu Racing Park", True, 52.1, 21.0, [])
    assert not by_id["Tor Kielce"].track_known
    assert repo.list_events(tracks=["Gazuu Racing Park"])  # filtr toru widzi nową nazwę

    # usunięcie toru z listy -> termin znów bez współrzędnych
    assert repo.relink_tracks(build_tracks([])) == 1
    g = repo.list_events(tracks=["Gazuu Racing Park"])[0]
    assert (g.track_known, g.lat) == (False, None) and "tor spoza listy znanych torów" in g.issues


def test_agent_uses_tracks_from_db():
    repo = EventRepository(":memory:")
    repo.save_tracks([GAZUU])

    def responder(system, user):
        return ExtractionResult(events=[ev(name="PIT BIKE", date_start="2027-05-01", track="Gazuu",
                                           url="https://x.example/e")], warnings=[], suspicious_instructions=False)

    agent = EventScoutAgent(FakeLLM(responder), settings=Settings(api_key=None), repository=repo)
    report = agent.run(ScoutInput(pasted_text="PIT BIKE 1 maja 2027, Gazuu", reference_date=date(2027, 1, 1)))
    e = report.events[0]
    assert (e.track, e.track_known, e.lat) == ("Gazuu Racing Park", True, 52.1)


def test_tracks_from_table_rows():
    from trackday.tools.normalize import tracks_from_rows

    nan = float("nan")
    rows = [
        {"Nazwa": "Tor Poznań", "Kraj": "PL", "Współrzędne (szer., dł.)": "52.41760, 16.80890",
         "Inne nazwy (po przecinku)": "Poznań, Przeźmierowo"},
        {"Nazwa": "Gazuu Racing Park", "Kraj": None, "Współrzędne (szer., dł.)": "52,1; 21,05",
         "Inne nazwy (po przecinku)": nan},                        # nowy wiersz: puste komórki = None / NaN
        {"Nazwa": nan, "Kraj": nan, "Współrzędne (szer., dł.)": nan, "Inne nazwy (po przecinku)": nan},  # pusty
    ]
    tracks, errors = tracks_from_rows(rows)
    assert errors == []
    assert [(t.name, t.country, t.lat, t.lon, t.aliases) for t in tracks] == [
        ("Tor Poznań", "PL", 52.4176, 16.8089, ["Poznań", "Przeźmierowo"]),
        ("Gazuu Racing Park", None, 52.1, 21.05, []),
    ]

    bad = [{"Nazwa": "A", "Współrzędne (szer., dł.)": "abc"}, {"Nazwa": "", "Współrzędne (szer., dł.)": "1, 2"},
           {"Nazwa": "Tor Poznan", "Współrzędne (szer., dł.)": "1, 2"}, {"Nazwa": "tor poznań", "Współrzędne (szer., dł.)": "1, 2"}]
    tracks, errors = tracks_from_rows(bad)
    assert len(errors) == 3 and "Wiersz 1 (A)" in errors[0] and "brak nazwy" in errors[1] and "już na liście" in errors[2]
