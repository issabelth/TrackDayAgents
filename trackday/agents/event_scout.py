"""Agent 1 - Poszukiwacz Terminów (Event Scout).

Rola: zbieranie informacji o torowaniach ze stron organizatorów.

Przepływ dla każdego źródła:
    fetch_source -> html_to_text -> detect_injection -> sanitize -> (chunking)
    -> LLM (structured output: ExtractionResult) -> walidacja/normalizacja -> TrackDayEvent
Na końcu: deduplikacja między źródłami i (opcjonalnie) zapis do SQLite.
"""

from __future__ import annotations

import uuid
from datetime import date, datetime
from urllib.parse import urljoin

from pydantic import BaseModel, Field, ValidationError

from trackday.agents.base import BaseAgent
from trackday.agents.prompts import EVENT_SCOUT_SYSTEM, EVENT_SCOUT_USER_TEMPLATE
from trackday.config import Settings, get_settings
from trackday.llm import LLMError, StructuredLLM
from trackday.models import (
    AgentStep,
    ExtractedEvent,
    ExtractionResult,
    ScoutReport,
    Source,
    SourceReport,
    TrackDayEvent,
)
from trackday.storage.db import EventRepository
from trackday.tools.fetch import FetchError, fetch_source
from trackday.tools.html_clean import html_to_text, split_into_chunks
from trackday.tools.normalize import deduplicate, event_id, match_track
from trackday.tools.security import detect_injection, same_site, sanitize_text, wrap_untrusted

# Przybliżone kursy do przeliczeń w PoC (do zastąpienia API kursów walut).
FX_TO_PLN = {"PLN": 1.0, "EUR": 4.30, "CZK": 0.17, "HUF": 0.011}


class ScoutInput(BaseModel):
    sources: list[Source] = Field(default_factory=list)
    pasted_text: str | None = None
    reference_date: date = Field(default_factory=date.today)


class EventScoutAgent(BaseAgent[ScoutInput, ScoutReport]):
    name = "Poszukiwacz Terminów (Event Scout)"
    role = "Zbieranie informacji o torowaniach ze stron organizatorów."

    def __init__(self, llm: StructuredLLM, settings: Settings | None = None,
                 repository: EventRepository | None = None):
        self.llm = llm
        self.settings = settings or get_settings()
        self.repository = repository

    # ------------------------------------------------------------------ API
    def run(self, data: ScoutInput) -> ScoutReport:
        report = ScoutReport(
            run_id=uuid.uuid4().hex[:8],
            started_at=datetime.now(),
            model=self.llm.model,
            reference_date=data.reference_date,
        )
        sources = [s for s in data.sources if s.enabled]
        if data.pasted_text and data.pasted_text.strip():
            sources.append(Source(id="pasted", name="Tekst wklejony przez użytkownika", url="text://pasted",
                                  kind="text"))
        if not sources:
            report.steps.append(AgentStep(tool="agent", detail="Brak źródeł do przeszukania.", ok=False))
            report.finished_at = datetime.now()
            return report

        all_events: list[TrackDayEvent] = []
        for src in sources:
            src_report, events = self._process_source(src, data, report)
            report.sources.append(src_report)
            all_events.extend(events)

        own = {s.id: s.organizer for s in sources if s.organizer}
        report.events, report.duplicates_removed = deduplicate(all_events, own)
        report.steps.append(AgentStep(
            tool="deduplicate",
            detail=f"{len(all_events)} wydarzeń -> {len(report.events)} po usunięciu "
                   f"{report.duplicates_removed} duplikatów."))

        if self.repository is not None:
            self.repository.save_run(report)
            report.steps.append(AgentStep(tool="sqlite", detail=f"Zapisano {len(report.events)} wydarzeń w bazie."))
        report.finished_at = datetime.now()
        return report

    # ------------------------------------------------------------ internals
    def _process_source(self, src: Source, data: ScoutInput, report: ScoutReport
                        ) -> tuple[SourceReport, list[TrackDayEvent]]:
        sr = SourceReport(source_id=src.id, source_name=src.name)

        # 1. Pobranie treści
        if src.kind == "text":
            text, warns = sanitize_text(data.pasted_text or "", self.settings.max_pasted_chars)
            sr.warnings.extend(warns)
            base_url = None
            hidden_text = ""
            report.steps.append(AgentStep(tool="input", detail=f"{src.name}: {len(text)} znaków."))
        else:
            try:
                fetched = fetch_source(src, self.settings)
            except FetchError as e:
                sr.ok, sr.error = False, str(e)
                report.steps.append(AgentStep(tool="fetch_source", detail=f"{src.name}: {e}", ok=False))
                return sr, []
            report.steps.append(AgentStep(tool="fetch_source",
                                          detail=f"{src.name}: pobrano {len(fetched.content)} znaków."))
            base_url = src.base_url or (fetched.final_url if fetched.final_url.startswith("http") else None)
            cleaned = html_to_text(fetched.content, base_url)
            text, warns = sanitize_text(cleaned.text, self.settings.max_pasted_chars * 4)
            sr.warnings.extend(warns)
            hidden_text = cleaned.hidden_text
            report.steps.append(AgentStep(
                tool="html_to_text",
                detail=f"{src.name}: {len(text)} znaków tekstu, usunięto {cleaned.hidden_elements} ukrytych elementów."))

        # 2. Heurystyczna detekcja prompt injection (tylko ostrzeżenie - dane i tak idą jako niezaufane)
        hits = detect_injection(text) + [f"(ukryty element) {h}" for h in detect_injection(hidden_text)]
        if hits:
            sr.suspicious_content = True
            sr.warnings.append(f"Wykryto {len(hits)} podejrzanych fragmentów (możliwy prompt injection): "
                               + " || ".join(h[:120] for h in hits[:3]))
            report.steps.append(AgentStep(tool="detect_injection", detail=f"{src.name}: {len(hits)} trafień.", ok=False))

        if not text.strip():
            sr.warnings.append("Brak treści do analizy.")
            return sr, []

        # 3. Ekstrakcja przez LLM (fragmentami, jeśli treść jest długa)
        chunks = split_into_chunks(text, getattr(self.llm, "max_chunk_chars", None) or self.settings.max_chunk_chars)
        sr.chunks = len(chunks)
        extracted: list[ExtractedEvent] = []
        for i, chunk in enumerate(chunks, start=1):
            chunk_info = f"Fragment {i} z {len(chunks)}." if len(chunks) > 1 else ""
            user = EVENT_SCOUT_USER_TEMPLATE.format(
                reference_date=data.reference_date.isoformat(),
                source_name=src.name,
                organizer=src.organizer or "nieznany",
                chunk_info=chunk_info,
                document=wrap_untrusted(chunk, src.name),
            )
            sr.chars_sent_to_llm += len(user)
            try:
                result = self.llm.extract(EVENT_SCOUT_SYSTEM, user, ExtractionResult)
            except LLMError as e:
                sr.ok, sr.error = False, str(e)
                report.steps.append(AgentStep(tool="llm_extract", detail=f"{src.name}: {e}", ok=False))
                return sr, []
            report.input_tokens += result.input_tokens
            report.output_tokens += result.output_tokens
            parsed: ExtractionResult = result.parsed
            extracted.extend(parsed.events)
            sr.warnings.extend(parsed.warnings)
            if parsed.suspicious_instructions:
                sr.suspicious_content = True
            report.steps.append(AgentStep(
                tool="llm_extract",
                detail=f"{src.name} [{i}/{len(chunks)}]: model {result.model} zwrócił {len(parsed.events)} wydarzeń "
                       f"({result.input_tokens} tok. wej. / {result.output_tokens} tok. wyj.)."))

        sr.raw_events = len(extracted)

        # 4. Walidacja i normalizacja
        events: list[TrackDayEvent] = []
        for ev in extracted:
            converted, problem = self._to_event(ev, src, base_url, data.reference_date)
            if converted is None:
                sr.rejected_events += 1
                sr.warnings.append(f"Odrzucono wpis '{ev.name}': {problem}")
            else:
                events.append(converted)
        sr.valid_events = len(events)
        report.steps.append(AgentStep(tool="validate",
                                      detail=f"{src.name}: {len(events)} poprawnych, {sr.rejected_events} odrzuconych."))
        return sr, events

    def _to_event(self, ev: ExtractedEvent, src: Source, base_url: str | None, ref: date
                  ) -> tuple[TrackDayEvent | None, str]:
        issues: list[str] = []
        try:
            start = date.fromisoformat(ev.date_start.strip())
        except (ValueError, AttributeError):
            return None, f"nieprawidłowa data '{ev.date_start}'"
        end = None
        if ev.date_end:
            try:
                end = date.fromisoformat(ev.date_end.strip())
            except ValueError:
                issues.append("nieprawidłowa data zakończenia - pominięto")
            if end is not None and end < start:
                issues.append("data zakończenia przed rozpoczęciem - pominięto")
                end = None
            if end == start:
                end = None
        if start < ref:
            issues.append("termin w przeszłości")

        if not ev.track or not ev.track.strip():
            return None, "brak nazwy toru"
        track = match_track(ev.track)
        if track is None:
            issues.append("tor spoza listy znanych torów")

        organizer = (ev.organizer or "").strip() or src.organizer or "nieznany"

        # Link: tylko http(s); dla stron organizatora - tylko ta sama domena (ochrona przed podmianą linków)
        page_url = base_url or (src.url if src.url.startswith("http") else None)
        url = (ev.url or "").strip()
        if url and base_url:
            url = urljoin(base_url, url)
        if url and not url.startswith(("http://", "https://")):
            issues.append("nieprawidłowy link - zastąpiono stroną źródła")
            url = ""
        if url and page_url and not same_site(url, page_url):
            issues.append(f"link do obcej domeny ({url}) - zastąpiono stroną źródła")
            url = ""
        if not url:
            if page_url:
                if not ev.url:
                    issues.append("brak linku do wydarzenia - użyto strony organizatora")
                url = page_url
            else:
                return None, "brak linku do wydarzenia"

        price = ev.price if ev.price is not None and ev.price >= 0 else None
        if ev.price is not None and ev.price < 0:
            issues.append("ujemna cena - pominięto")
        currency = (ev.currency or "").upper().strip() or None
        if currency in ("ZŁ", "ZL", "PLZ"):
            currency = "PLN"
        if price is not None and currency is None:
            currency = "PLN"
            issues.append("brak waluty - przyjęto PLN")
        price_pln = None
        if price is not None and currency in FX_TO_PLN:
            price_pln = round(price * FX_TO_PLN[currency], 2)
            if currency != "PLN":
                issues.append(f"cena przeliczona z {currency} po kursie orientacyjnym")
        if price is None:
            issues.append("brak ceny")

        seats_left = ev.seats_left if ev.seats_left is None or ev.seats_left >= 0 else None
        seats_total = ev.seats_total if ev.seats_total is None or ev.seats_total > 0 else None
        if seats_left is not None and seats_total is not None and seats_left > seats_total:
            issues.append("wolnych miejsc więcej niż puli - pominięto pulę")
            seats_total = None
        if seats_left is None and not ev.availability_note:
            issues.append("brak informacji o miejscach")

        if ev.confidence < 0.7:
            issues.append("niska pewność ekstrakcji")

        try:
            return TrackDayEvent(
                id=event_id(organizer, track.name if track else ev.track, start),
                name=ev.name.strip() or f"Torowanie {ev.track}",
                date_start=start,
                date_end=end,
                track=track.name if track else ev.track.strip(),
                track_known=track is not None,
                organizer=organizer,
                price=price,
                currency=currency if price is not None else None,
                price_pln=price_pln,
                price_note=ev.price_note,
                groups=[g.strip() for g in ev.groups if g and g.strip()],
                url=url,
                seats_left=seats_left,
                seats_total=seats_total,
                availability_note=ev.availability_note,
                confidence=ev.confidence,
                lat=track.lat if track else None,
                lon=track.lon if track else None,
                source_id=src.id,
                issues=issues + ([f"uwaga modelu: {ev.notes}"] if ev.notes else []),
            ), ""
        except ValidationError as e:
            return None, "; ".join(err["msg"] for err in e.errors())
