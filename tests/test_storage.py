from datetime import date

from trackday.evaluation import evaluate, golden_as_events, load_golden
from trackday.models import ScoutReport
from trackday.storage.db import EventRepository


def make_repo():
    repo = EventRepository(":memory:")
    repo.save_run(ScoutReport(run_id="t1", started_at="2026-11-14T10:00:00", model="x",
                              reference_date=date(2026, 11, 14), events=golden_as_events()))
    return repo


def test_filters():
    repo = make_repo()
    assert len(repo.list_events()) == 20
    june = repo.list_events(date_from=date(2027, 6, 1), date_to=date(2027, 6, 30))
    assert {e.date_start.month for e in june} == {6} and len(june) == 4
    cheap = repo.list_events(max_price_pln=450)
    assert all(e.price_pln is None or e.price_pln <= 450 for e in cheap)
    poz = repo.list_events(tracks=["Tor Poznań"], only_available=True)
    assert poz and all(e.track == "Tor Poznań" and e.seats_left != 0 for e in poz)
    assert "Speed Academy" in repo.distinct("organizer")


def test_upsert_keeps_seat_history():
    repo = make_repo()
    ev = repo.list_events()[0]
    repo.upsert_events([ev.model_copy(update={"seats_left": 1})])
    assert len(repo.list_events()) == 20
    assert repo.list_events()[0].seats_left == 1


def test_golden_evaluates_to_100_percent():
    _, golden = load_golden()
    res = evaluate(golden_as_events(), golden)
    assert res.precision == res.recall == 1.0 and not res.mismatches


def ev(i, src="a", day=date(2027, 5, 1), org="X", **kw):
    from trackday.models import TrackDayEvent

    return TrackDayEvent(**({"id": i, "name": i, "date_start": day, "track": "Tor Poznań", "organizer": org,
                             "url": "https://x.example/", "source_id": src} | kw))


def run(events, sources=()):
    from datetime import datetime

    from trackday.models import SourceReport

    return ScoutReport(run_id=str(id(events)), started_at=datetime.now(), model="m", reference_date=date(2027, 1, 1),
                       events=events, sources=[SourceReport(source_id=s, source_name=s, ok=ok) for s, ok in sources])


def test_saved_events_stay_and_missing_ones_are_marked():
    repo = EventRepository(":memory:")
    repo.save_run(run([ev("old", day=date(2027, 5, 1)), ev("keep", day=date(2027, 5, 2)),
                       ev("other", "b", day=date(2027, 5, 3))]))
    repo.save_run(run([ev("keep", day=date(2027, 5, 2))], [("a", True), ("b", False)]))
    stored = {x.id: x for x in repo.list_events()}
    assert sorted(stored) == ["keep", "old", "other"]  # nic nie znika z bazy
    assert stored["old"].missing_since is not None  # udany przebieg źródła a go nie znalazł
    assert stored["keep"].missing_since is None
    assert stored["other"].missing_since is None  # źródło b nie zadziałało - brak podstaw do oznaczenia
    assert [x.id for x in repo.list_events(hide_missing=True)] == ["keep", "other"]
    # termin wraca na stronę -> znacznik znika
    repo.save_run(run([ev("old", day=date(2027, 5, 1))], [("a", True)]))
    assert repo.list_events(date_to=date(2027, 5, 1))[0].missing_since is None


def test_repeated_search_updates_instead_of_duplicating():
    repo = EventRepository(":memory:")
    repo.save_run(run([ev("id-1", org="Speed Academy", price=500, currency="PLN", price_pln=500, seats_left=10)]))
    first = repo.list_events()[0]
    # kolejne wyszukiwanie: model zapisał organizatora inaczej, inne id, brak ceny, mniej miejsc
    res = repo.save_run(run([ev("id-2", org="SPEED ACADEMY Sp. z o.o.", seats_left=4)]))
    assert (res.added, res.updated) == (0, 1)
    [e] = repo.list_events()
    assert e.id == "id-1" and e.seats_left == 4
    assert e.price_pln == 500  # cena z poprzedniego zapisu została
    assert e.first_seen == first.first_seen
    assert [s for _, s in repo.seat_history("id-1")][-1] == 4


def test_other_organizer_or_session_is_a_new_event():
    repo = EventRepository(":memory:")
    repo.save_run(run([ev("rano", start_time="09:00")]))
    res = repo.save_run(run([ev("rano", start_time="14:00"),  # to samo id, ale inna sesja
                             ev("y", org="Inny Organizator")]))
    assert (res.added, res.updated) == (2, 0)
    assert len(repo.list_events()) == 3 and len({e.id for e in repo.list_events()}) == 3


def test_price_and_weekday_filters():
    repo = make_repo()
    mid = repo.list_events(min_price_pln=400, max_price_pln=600)
    assert all(e.price_pln is None or 400 <= e.price_pln <= 600 for e in mid)
    weekend = repo.list_events(weekdays=[5, 6])
    assert weekend and all(e.date_start.weekday() >= 5 for e in weekend)
    assert len(weekend) == sum(e.date_start.weekday() >= 5 for e in repo.list_events())


def test_old_database_gets_new_column(tmp_path):
    import sqlite3

    db = tmp_path / "old.db"
    with sqlite3.connect(db) as c:
        c.execute("CREATE TABLE events (id TEXT PRIMARY KEY, date_start TEXT NOT NULL, date_end TEXT, track TEXT "
                  "NOT NULL, organizer TEXT NOT NULL, price_pln REAL, seats_left INTEGER, data TEXT NOT NULL, "
                  "first_seen TEXT NOT NULL, last_seen TEXT NOT NULL)")
    repo = EventRepository(db)
    repo.save_run(run([ev("a")]))
    assert repo.list_events(hide_missing=True)[0].id == "a"


def test_multi_day_event_matches_filters_by_any_of_its_days():
    repo = EventRepository(":memory:")
    # pt 30.04 - nd 02.05.2027: zaczyna się w piątek i w kwietniu
    repo.save_run(run([ev("weekend", day=date(2027, 4, 30), date_end=date(2027, 5, 2)),
                       ev("friday", day=date(2027, 4, 23))]))
    assert [e.id for e in repo.list_events(weekdays=[5, 6])] == ["weekend"]
    assert [e.id for e in repo.list_events(date_from=date(2027, 5, 1))] == ["weekend"]
    assert [e.id for e in repo.list_events(date_to=date(2027, 4, 29))] == ["friday"]
