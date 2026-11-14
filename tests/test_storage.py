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
