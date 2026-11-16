"""Agent 1 - Poszukiwacz Terminów (Event Scout).

Rola: zbieranie informacji o torowaniach ze stron organizatorów.

Przepływ dla każdego źródła:
    fetch_source -> [tryb podstron: find_detail_links -> fetch_detail dla każdej podstrony]
    -> html_to_text (+ extract_variants: warianty sesja × grupa ze sklepu) -> detect_injection -> sanitize -> (chunking)
    -> LLM (structured output: ExtractionResult) -> walidacja/normalizacja -> TrackDayEvent
Na końcu: deduplikacja między źródłami i (opcjonalnie) zapis do SQLite.
"""

from __future__ import annotations

import re
import time
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
    EventVariant,
    ExtractedEvent,
    ExtractionResult,
    ScoutReport,
    Source,
    SourceReport,
    TrackDayEvent,
)
from trackday.storage.db import EventRepository
from trackday.tools.fetch import FetchError, fetch_source, fetch_url
from trackday.tools.html_clean import extract_links, html_to_text, split_into_chunks
from trackday.tools.normalize import build_tracks, deduplicate, default_track_defs, event_id, match_track
from trackday.tools.variants import extract_variants
from trackday.tools.security import detect_injection, same_site, sanitize_text, wrap_untrusted

# Przybliżone kursy do przeliczeń w PoC (do zastąpienia API kursów walut).
FX_TO_PLN = {"PLN": 1.0, "EUR": 4.30, "CZK": 0.17, "HUF": 0.011}


_TIME_RX = re.compile(r"(?<![\d.])([01]?\d|2[0-3])[.:]([0-5]\d)(?![\d.])")
_TIME_HINT_RX = re.compile(r"(?:godz\.?|godzina|start|o)\s*([01]?\d|2[0-3])[.:]([0-5]\d)(?![\d.])", re.I)


def _parse_time(text: str | None, require_hint: bool = False) -> str | None:
    """'13.30' / 'godz. 13:30' -> '13:30'. Z nazwy wydarzenia tylko po słowie 'godz.' (żeby nie brać dat)."""
    if not text:
        return None
    m = (_TIME_HINT_RX if require_hint else _TIME_RX).search(text)
    return f"{int(m.group(1)):02d}:{m.group(2)}" if m else None


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
        # Lista znanych torów: z bazy (edytowana w zakładce „Tory”), a bez bazy - z data/tracks.json
        self._tracks = None
        if self.repository is not None:
            self.repository.init_tracks(default_track_defs())
            self._tracks = build_tracks(self.repository.list_tracks())
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

        # 1. Pobranie treści -> lista stron do analizy: (etykieta, tekst, ukryty tekst, adres strony, warianty)
        pages: list[tuple[str, str, str, str | None, list[EventVariant]]] = []
        detail_mode = False
        if src.kind == "text":
            text, warns = sanitize_text(data.pasted_text or "", self.settings.max_pasted_chars)
            sr.warnings.extend(warns)
            pages.append((src.name, text, "", None, []))
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
            links = self._detail_links(src, fetched.content, base_url, sr, report) if src.detail_link_pattern else []
            if links:
                detail_mode = True
                pages.extend(self._fetch_details(src, links, sr, report))
            else:
                pages.append(self._clean_page(src.name, fetched.content, base_url, sr, report))

        # 2-3. Detekcja prompt injection i ekstrakcja przez LLM (każda strona osobno, długie - fragmentami)
        extracted: list[tuple[ExtractedEvent, str | None, list[EventVariant]]] = []
        for label, text, hidden_text, page_url, variants in pages:
            got = self._extract_page(src, label, text, hidden_text, data, sr, report)
            if got is None:  # błąd modelu - przerywamy to źródło
                return sr, []
            # Warianty (sesja × grupa) dotyczą wydarzenia ze strony - przypisujemy je, gdy wydarzenie jest jedno
            if variants and len(got) != 1:
                sr.warnings.append(f"{label}: model zwrócił {len(got)} wydarzeń, a strona ma {len(variants)} "
                                   f"wariantów - warianty pominięto.")
                variants = []
            extracted.extend((ev, page_url, variants) for ev in got)
        sr.raw_events = len(extracted)

        # 4. Walidacja i normalizacja
        events: list[TrackDayEvent] = []
        for ev, page_url, variants in extracted:
            converted, problem = self._to_event(ev, src, page_url, data.reference_date, detail_page=detail_mode,
                                                variants=variants)
            if converted is None:
                sr.rejected_events += 1
                sr.warnings.append(f"Odrzucono wpis '{ev.name}': {problem}")
            else:
                events.append(converted)
        sr.valid_events = len(events)
        report.steps.append(AgentStep(tool="validate",
                                      detail=f"{src.name}: {len(events)} poprawnych, {sr.rejected_events} odrzuconych."))
        return sr, events

    def _clean_page(self, label: str, html: str, page_url: str | None, sr: SourceReport,
                    report: ScoutReport) -> tuple[str, str, str, str | None, list[EventVariant]]:
        cleaned = html_to_text(html, page_url)
        text, warns = sanitize_text(cleaned.text, self.settings.max_pasted_chars * 4)
        sr.warnings.extend(warns)
        report.steps.append(AgentStep(
            tool="html_to_text",
            detail=f"{label}: {len(text)} znaków tekstu, usunięto {cleaned.hidden_elements} ukrytych elementów."))
        found = extract_variants(html)
        sr.warnings.extend(f"{label}: {w}" for w in found.warnings)
        if found.variants:
            report.steps.append(AgentStep(
                tool="extract_variants",
                detail=f"{label}: {len(found.variants)} wariantów (sesja × grupa) z liczbą miejsc."))
        return label, text, cleaned.hidden_text, page_url, found.variants

    def _detail_links(self, src: Source, html: str, base_url: str | None, sr: SourceReport,
                      report: ScoutReport) -> list[str]:
        """Tryb 'lista -> podstrony': linki do podstron wydarzeń pasujące do wzorca źródła."""
        if not base_url or not base_url.startswith("http"):
            sr.warnings.append("Tryb podstron wymaga adresu http(s) - analizuję tylko stronę główną źródła.")
            return []
        pattern = src.detail_link_pattern or ""
        try:
            rx = re.compile(pattern, re.I)
        except re.error:
            rx = re.compile(re.escape(pattern), re.I)  # zwykły fragment adresu zamiast wyrażenia regularnego
        own = {base_url.rstrip("/"), src.url.rstrip("/")}
        links = [u for u in extract_links(html, base_url)
                 if u.startswith(("http://", "https://")) and same_site(u, base_url)
                 and u.rstrip("/") not in own and rx.search(u)]
        report.steps.append(AgentStep(
            tool="find_detail_links",
            detail=f"{src.name}: {len(links)} linków pasujących do wzorca '{pattern}'.", ok=bool(links)))
        if not links:
            sr.warnings.append(f"Żaden link nie pasuje do wzorca '{pattern}' - analizuję tylko stronę główną źródła.")
        elif len(links) > src.max_detail_pages:
            sr.warnings.append(f"Znaleziono {len(links)} podstron - odczytano pierwsze {src.max_detail_pages} "
                               f"(limit ustawiony dla źródła).")
            links = links[:src.max_detail_pages]
        return links

    def _fetch_details(self, src: Source, links: list[str], sr: SourceReport, report: ScoutReport
                       ) -> list[tuple[str, str, str, str | None, list[EventVariant]]]:
        pages = []
        robots: dict = {}
        for i, url in enumerate(links):
            if i:
                time.sleep(self.settings.crawl_delay_s)
            try:
                fetched = fetch_url(url, self.settings, robots)
            except FetchError as e:
                sr.warnings.append(f"Nie pobrano podstrony {url}: {e}")
                report.steps.append(AgentStep(tool="fetch_detail", detail=f"{url}: {e}", ok=False))
                continue
            sr.detail_pages += 1
            pages.append(self._clean_page(f"{src.name} → {url}", fetched.content, fetched.final_url, sr, report))
        report.steps.append(AgentStep(tool="fetch_detail",
                                      detail=f"{src.name}: pobrano {sr.detail_pages} z {len(links)} podstron."))
        return pages

    def _extract_page(self, src: Source, label: str, text: str, hidden_text: str, data: ScoutInput,
                      sr: SourceReport, report: ScoutReport) -> list[ExtractedEvent] | None:
        # Heurystyczna detekcja prompt injection (tylko ostrzeżenie - dane i tak idą jako niezaufane)
        hits = detect_injection(text) + [f"(ukryty element) {h}" for h in detect_injection(hidden_text)]
        if hits:
            sr.suspicious_content = True
            sr.warnings.append(f"Wykryto {len(hits)} podejrzanych fragmentów (możliwy prompt injection): "
                               + " || ".join(h[:120] for h in hits[:3]))
            report.steps.append(AgentStep(tool="detect_injection", detail=f"{label}: {len(hits)} trafień.", ok=False))

        if not text.strip():
            sr.warnings.append(f"{label}: brak treści do analizy.")
            return []

        chunks = split_into_chunks(text, getattr(self.llm, "max_chunk_chars", None) or self.settings.max_chunk_chars)
        sr.chunks += len(chunks)
        extracted: list[ExtractedEvent] = []
        for i, chunk in enumerate(chunks, start=1):
            chunk_info = f"Fragment {i} z {len(chunks)}." if len(chunks) > 1 else ""
            user = EVENT_SCOUT_USER_TEMPLATE.format(
                reference_date=data.reference_date.isoformat(),
                source_name=label,
                organizer=src.organizer or "nieznany",
                chunk_info=chunk_info,
                document=wrap_untrusted(chunk, label),
            )
            sr.chars_sent_to_llm += len(user)
            try:
                result = self.llm.extract(EVENT_SCOUT_SYSTEM, user, ExtractionResult)
            except LLMError as e:
                sr.ok, sr.error = False, str(e)
                report.steps.append(AgentStep(tool="llm_extract", detail=f"{label}: {e}", ok=False))
                return None
            report.input_tokens += result.input_tokens
            report.output_tokens += result.output_tokens
            parsed: ExtractionResult = result.parsed
            extracted.extend(parsed.events)
            sr.warnings.extend(parsed.warnings)
            if parsed.suspicious_instructions:
                sr.suspicious_content = True
            report.steps.append(AgentStep(
                tool="llm_extract",
                detail=f"{label} [{i}/{len(chunks)}]: model {result.model} zwrócił {len(parsed.events)} wydarzeń "
                       f"({result.input_tokens} tok. wej. / {result.output_tokens} tok. wyj.)."))
        return extracted

    def _to_event(self, ev: ExtractedEvent, src: Source, base_url: str | None, ref: date,
                  detail_page: bool = False, variants: list[EventVariant] | None = None
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

        start_time = _parse_time(ev.start_time) or _parse_time(ev.name, require_hint=True)
        track_layout = (ev.track_layout or "").strip()[:60] or None
        if track_layout is None and re.search(r"\brevers(e)?\b", ev.name, re.I):
            track_layout = "REVERS"

        if not ev.track or not ev.track.strip():
            return None, "brak nazwy toru"
        track = match_track(ev.track, getattr(self, "_tracks", None))
        if track is None:
            issues.append("tor spoza listy znanych torów")

        organizer = (ev.organizer or "").strip() or src.organizer or "nieznany"

        # Link: tylko http(s); dla stron organizatora - tylko ta sama domena (ochrona przed podmianą linków)
        page_url = base_url or (src.url if src.url.startswith("http") else None)
        # na podstronie jednego wydarzenia jej adres JEST linkiem do wydarzenia (model bywa mylony
        # linkami „udostępnij na Facebooku” itp.)
        url = page_url if detail_page and page_url else (ev.url or "").strip()
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
                if not ev.url and not detail_page:
                    issues.append("brak linku do wydarzenia - użyto strony organizatora")
                url = page_url
            else:
                return None, "brak linku do wydarzenia"

        variants = variants or []
        price = ev.price if ev.price is not None and ev.price >= 0 else None
        v_prices = [v.price for v in variants if v.price is not None]
        if v_prices:  # cena ze sklepu (najniższa z wariantów) jest pewniejsza niż odczyt modelu
            price, ev.currency = min(v_prices), next(v.currency for v in variants if v.price == min(v_prices))
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
        groups = [g.strip() for g in ev.groups if g and g.strip()]
        availability_note = ev.availability_note
        if variants:
            # suma wolnych miejsc wszystkich wariantów; grupy = grupy, które naprawdę można wybrać
            known = [v.seats_left for v in variants if v.seats_left is not None]
            seats_left = sum(known) if len(known) == len(variants) else seats_left
            seats_total = None if len(known) == len(variants) else seats_total
            groups = list(dict.fromkeys(v.group for v in variants if v.group)) or groups
            availability_note = f"{len(variants)} wariantów (sesja × grupa) - szczegóły po rozwinięciu"
        if seats_left is None and not availability_note:
            issues.append("brak informacji o miejscach")

        if ev.confidence < 0.7:
            issues.append("niska pewność ekstrakcji")

        try:
            return TrackDayEvent(
                # adres podstrony odróżnia sesje tego samego dnia na tym samym torze (np. rano / po południu)
                id=event_id(organizer, track.name if track else ev.track, start, extra=url if detail_page else ""),
                name=ev.name.strip() or f"Torowanie {ev.track}",
                date_start=start,
                date_end=end,
                track=track.name if track else ev.track.strip(),
                start_time=start_time,
                track_layout=track_layout,
                track_known=track is not None,
                organizer=organizer,
                price=price,
                currency=currency if price is not None else None,
                price_pln=price_pln,
                price_note=ev.price_note,
                groups=groups,
                url=url,
                seats_left=seats_left,
                seats_total=seats_total,
                availability_note=availability_note,
                confidence=ev.confidence,
                lat=track.lat if track else None,
                lon=track.lon if track else None,
                source_id=src.id,
                issues=issues + ([f"uwaga modelu: {ev.notes}"] if ev.notes else []),
                variants=variants,
            ), ""
        except ValidationError as e:
            return None, "; ".join(err["msg"] for err in e.errors())
