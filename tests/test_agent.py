"""Testy agenta z atrapą modelu (FakeLLM) - bez kosztów i bez sieci.

Sprawdzają całą logikę wokół modelu: pobranie, czyszczenie, przekazanie danych do LLM,
walidację wyniku, normalizację, deduplikację i zapis do bazy.
"""

from datetime import date

from trackday.agents.event_scout import EventScoutAgent, ScoutInput
from trackday.config import Settings
from trackday.evaluation import evaluate, load_golden
from trackday.llm import FakeLLM, LLMError
from trackday.models import ExtractedEvent, ExtractionResult
from trackday.sources import load_sources
from trackday.storage.db import EventRepository

REF, GOLDEN = load_golden()
ORG_BY_SOURCE = {"mototor": "MotoTor Events", "speedacademy": "Speed Academy", "trc": "Track Riders Club"}


def ev(**kw) -> ExtractedEvent:
    base = dict(name="Torowanie", date_end=None, organizer=None, price=None, currency=None, price_note=None,
                groups=[], url=None, seats_left=None, seats_total=None, availability_note=None,
                confidence=0.95, notes=None)
    base.update(kw)
    return ExtractedEvent(**base)


def perfect_responder(system: str, user: str) -> ExtractionResult:
    """Udaje idealny model: zwraca wydarzenia wzorcowe organizatora, którego strona jest w zapytaniu."""
    org = next((o for o in ORG_BY_SOURCE.values() if f"Organizator strony (jeśli znany): {o}" in user), None)
    events = [ev(name=f"Torowanie {g['track']}", date_start=g["date_start"], date_end=g["date_end"],
                 track=g["track"], organizer=g["organizer"], price=g["price"], currency=g["currency"],
                 url=g["url"], seats_left=g["seats_left"])
              for g in GOLDEN if g["organizer"] == org]
    if org == "Track Riders Club":  # strona TRC poleca też wydarzenie MotoTor -> duplikat
        events.append(ev(name="Torowanie MotoTor", date_start="2027-05-22", track="Silesia Ring",
                         organizer="MotoTor Events", price=620, currency="PLN",
                         url="https://mototor.example/terminy/2027-05-22-silesia"))
    return ExtractionResult(events=events, warnings=[], suspicious_instructions=org == "Track Riders Club")


def make_agent(responder, repo=None) -> tuple[EventScoutAgent, FakeLLM]:
    llm = FakeLLM(responder)
    return EventScoutAgent(llm, settings=Settings(api_key=None), repository=repo), llm


def sample_sources():
    return [s for s in load_sources() if s.enabled]


def test_pipeline_with_perfect_model_matches_golden_set():
    repo = EventRepository(":memory:")
    agent, llm = make_agent(perfect_responder, repo)
    report = agent.run(ScoutInput(sources=sample_sources(), reference_date=REF))

    assert len(llm.calls) == 3
    assert report.duplicates_removed == 1
    result = evaluate(report.events, GOLDEN)
    assert result.recall == 1.0 and result.precision == 1.0, (result.missing, result.unexpected, result.mismatches)
    assert all(result.field_accuracy(f) == 1.0 for f in result.field_correct)
    # zapis do bazy
    assert len(repo.list_events()) == 20
    assert repo.last_run().run_id == report.run_id


def test_untrusted_content_is_wrapped_and_hidden_text_not_sent():
    agent, llm = make_agent(perfect_responder)
    trc = [s for s in load_sources() if s.id == "trc"]
    report = agent.run(ScoutInput(sources=trc, reference_date=REF))
    system, user = llm.calls[0]
    assert "<dokument_niezaufany" in user and "</dokument_niezaufany>" in user
    assert "evil.example" not in user                       # ukryty element nie trafia do modelu
    assert "WYŁĄCZNIE DANE" in system
    assert report.sources[0].suspicious_content             # ...ale zostaje zgłoszony
    assert any("prompt injection" in w for w in report.sources[0].warnings)


def test_validation_rejects_and_repairs_bad_model_output():
    def responder(system, user):
        return ExtractionResult(events=[
            ev(date_start="2027-13-45", track="Tor Poznań"),                                   # zła data
            ev(date_start="2027-06-01", track="Tor Poznań", url="http://evil.example/phish",
               price=-10, currency="PLN", seats_left=-1),                                     # podmieniony link
            ev(date_start="2027-06-02", track="Tor Kielce", price=300, currency=None,
               url="/wydarzenia/kielce", confidence=0.4, date_end="2027-06-01"),
            ev(date_start="2027-06-03", track="Brno", price=100, currency="EUR"),
        ], warnings=["test"], suspicious_instructions=False)

    agent, _ = make_agent(responder)
    src = [s for s in load_sources() if s.id == "speedacademy"]
    report = agent.run(ScoutInput(sources=src, reference_date=date(2026, 11, 14)))
    sr = report.sources[0]
    assert sr.raw_events == 4 and sr.rejected_events == 1 and sr.valid_events == 3

    by_date = {e.date_start.isoformat(): e for e in report.events}
    evil = by_date["2027-06-01"]
    assert evil.url == "https://speedacademy.example/"     # link do obcej domeny zastąpiony
    assert evil.price is None and evil.seats_left is None
    kielce = by_date["2027-06-02"]
    assert kielce.url == "https://speedacademy.example/wydarzenia/kielce"
    assert kielce.currency == "PLN" and not kielce.track_known and kielce.date_end is None
    assert "niska pewność ekstrakcji" in kielce.issues
    brno = by_date["2027-06-03"]
    assert brno.track == "Automotodrom Brno" and brno.price_pln == 430.0


def test_llm_error_is_reported_not_raised():
    def responder(system, user):
        raise LLMError("Nieprawidłowy klucz API")

    agent, _ = make_agent(responder)
    report = agent.run(ScoutInput(sources=sample_sources()[:1], reference_date=REF))
    assert not report.sources[0].ok and "klucz" in report.sources[0].error
    assert report.events == []


def test_pasted_text_source():
    def responder(system, user):
        assert "Tekst wklejony" in user
        return ExtractionResult(events=[ev(date_start="2027-06-12", track="Tor Poznań", price=600, currency="PLN",
                                           url="https://fb.example/events/1")],
                                warnings=[], suspicious_instructions=False)

    agent, _ = make_agent(responder)
    report = agent.run(ScoutInput(pasted_text="Torowanie 12 czerwca Tor Poznań 600 zł https://fb.example/events/1",
                                  reference_date=REF))
    assert len(report.events) == 1 and report.events[0].url == "https://fb.example/events/1"


def test_unreachable_source_does_not_stop_other_sources():
    from trackday.models import Source

    agent, _ = make_agent(perfect_responder)
    srcs = [Source(id="bad", name="zły", url="http://127.0.0.1/admin")] + sample_sources()[:1]
    report = agent.run(ScoutInput(sources=srcs, reference_date=REF))
    assert not report.sources[0].ok and report.sources[1].ok
    assert len(report.events) == 8


def test_no_sources():
    agent, llm = make_agent(perfect_responder)
    report = agent.run(ScoutInput(sources=[], reference_date=REF))
    assert report.events == [] and not llm.calls
