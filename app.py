"""TrackDayAgents - aplikacja Streamlit (etap 1: Agent 1 - Poszukiwacz Terminów).

Uruchomienie:  streamlit run app.py
"""

from __future__ import annotations

from datetime import date, timedelta

import pandas as pd
import streamlit as st

from trackday.agents.event_scout import EventScoutAgent, ScoutInput
from trackday.agents.prompts import EVENT_SCOUT_SYSTEM
from trackday.config import AVAILABLE_MODELS, get_settings
from trackday.evaluation import golden_as_events
from trackday.llm import AnthropicLLM
from trackday.models import ExtractionResult, ScoutReport, TrackDayEvent
from trackday.sources import load_sources, sources_from_urls
from trackday.storage.db import EventRepository

st.set_page_config(page_title="TrackDay Agents", page_icon="🏍️", layout="wide")

settings = get_settings()


@st.cache_resource
def get_repo(path: str) -> EventRepository:
    return EventRepository(path)


repo = get_repo(str(settings.db_path))

# ============================================================== SIDEBAR
with st.sidebar:
    st.title("🏍️ TrackDay Agents")
    st.caption("Agent 1 - Poszukiwacz Terminów")

    st.subheader("1. Źródła")
    all_sources = load_sources()
    chosen = st.multiselect(
        "Organizatorzy (strony przykładowe)",
        options=[s.id for s in all_sources],
        default=[s.id for s in all_sources if s.enabled],
        format_func=lambda sid: next(s.name for s in all_sources if s.id == sid),
    )
    urls_text = st.text_area("Własne adresy stron (po jednym w linii)", placeholder="https://...", height=80)
    pasted = st.text_area("…lub wklej treść ogłoszenia / posta", height=120,
                          placeholder="np. treść posta z Facebooka z terminem torowania")

    st.subheader("2. Model")
    model = st.selectbox("Model językowy", list(AVAILABLE_MODELS), format_func=AVAILABLE_MODELS.get,
                         index=list(AVAILABLE_MODELS).index(settings.model) if settings.model in AVAILABLE_MODELS else 0)
    api_key = settings.api_key or st.text_input("ANTHROPIC_API_KEY", type="password",
                                                help="Klucz nie jest zapisywany. Najlepiej ustaw go w pliku .env.")
    ref_date = st.date_input("Data referencyjna", value=date.today(),
                             help="Względem niej agent uzupełnia brakujący rok w datach.")

    run_clicked = st.button("🔎 Szukaj terminów (uruchom agenta)", type="primary", use_container_width=True)
    demo_clicked = st.button("Tryb demo: wczytaj zestaw wzorcowy (bez LLM)", use_container_width=True)

    st.subheader("3. Filtry")
    today = date.today()
    d_range = st.date_input("Zakres dat", value=(today, today + timedelta(days=365)))
    max_price = st.slider("Maksymalny koszt [zł]", 0, 2000, 2000, step=50)
    track_filter = st.multiselect("Tory", repo.distinct("track"))
    org_filter = st.multiselect("Organizatorzy", repo.distinct("organizer"))
    only_available = st.checkbox("Tylko z wolnymi miejscami")
    if st.button("Wyczyść bazę", use_container_width=True):
        repo.clear()
        st.session_state.pop("report", None)
        st.rerun()

# ============================================================== AKCJE
if demo_clicked:
    events = golden_as_events()
    repo.save_run(ScoutReport(run_id="demo", started_at=pd.Timestamp.now().to_pydatetime(), model="demo (bez LLM)",
                              reference_date=ref_date, events=events))
    st.session_state["report"] = repo.last_run()
    st.toast(f"Wczytano {len(events)} wydarzeń demonstracyjnych.")
    st.rerun()

if run_clicked:
    srcs = [s for s in all_sources if s.id in chosen]
    srcs = [s.model_copy(update={"enabled": True}) for s in srcs] + sources_from_urls(urls_text)
    if not srcs and not pasted.strip():
        st.sidebar.error("Wybierz co najmniej jedno źródło lub wklej treść.")
    elif not api_key:
        st.sidebar.error("Brak klucza ANTHROPIC_API_KEY (plik .env lub pole powyżej).")
    else:
        agent = EventScoutAgent(AnthropicLLM(model=model, api_key=api_key, effort=settings.effort),
                                settings=settings, repository=repo)
        with st.status("Agent przeszukuje źródła…", expanded=True) as status:
            st.write(f"Źródła: {', '.join(s.name for s in srcs) or '-'}" + (" + wklejony tekst" if pasted.strip() else ""))
            report = agent.run(ScoutInput(sources=srcs, pasted_text=pasted or None, reference_date=ref_date))
            for step in report.steps:
                st.write(("✅ " if step.ok else "⚠️ ") + f"**{step.tool}** - {step.detail}")
            status.update(label=f"Gotowe: {len(report.events)} wydarzeń", state="complete")
        st.session_state["report"] = report

report: ScoutReport | None = st.session_state.get("report") or repo.last_run()


# ============================================================== WIDOK
def events_df(events: list[TrackDayEvent]) -> pd.DataFrame:
    rows = []
    for e in events:
        price = f"{e.price:g} {e.currency}" if e.price is not None else "-"
        seats = (str(e.seats_left) + (f" / {e.seats_total}" if e.seats_total else "")) if e.seats_left is not None \
            else (e.availability_note or "?")
        rows.append({
            "Data": e.date_start.strftime("%d.%m.%Y") + (f"–{e.date_end.strftime('%d.%m')}" if e.date_end else ""),
            "Tor": e.track, "Organizator": e.organizer, "Cena": price,
            "Cena [zł]": e.price_pln, "Wolne miejsca": seats, "Grupy": ", ".join(e.groups),
            "Pewność": e.confidence, "Link": e.url, "Uwagi": "; ".join(e.issues),
        })
    return pd.DataFrame(rows)


st.title("Torowania motocyklowe")
st.caption("Jedna lista terminów od wielu organizatorów - dane zebrane przez agenta „Poszukiwacz Terminów”.")

d_from, d_to = (d_range if isinstance(d_range, tuple) and len(d_range) == 2 else (None, None))
events = repo.list_events(date_from=d_from, date_to=d_to, max_price_pln=None if max_price >= 2000 else max_price,
                          tracks=track_filter or None, organizers=org_filter or None, only_available=only_available)

tab_events, tab_map, tab_report, tab_agent, tab_next = st.tabs(
    ["📅 Terminy", "🗺️ Mapa", "🧾 Raport agenta", "🤖 O agencie", "🔜 Obserwowane / Powiadomienia"])

with tab_events:
    c1, c2, c3, c4 = st.columns(4)
    c1.metric("Terminy (po filtrach)", len(events))
    c2.metric("Tory", len({e.track for e in events}))
    c3.metric("Organizatorzy", len({e.organizer for e in events}))
    low = [e for e in events if e.seats_left is not None and 0 < e.seats_left < 3]
    c4.metric("Prawie wyprzedane (<3 miejsca)", len(low))
    if not events:
        st.info("Brak terminów. Wybierz źródła w panelu bocznym i kliknij **Szukaj terminów** "
                "(albo wczytaj tryb demo).")
    else:
        df = events_df(events)
        st.dataframe(
            df, use_container_width=True, hide_index=True,
            column_config={
                "Link": st.column_config.LinkColumn("Link", display_text="otwórz ↗"),
                "Cena [zł]": st.column_config.NumberColumn(format="%.0f zł"),
                "Pewność": st.column_config.ProgressColumn(min_value=0.0, max_value=1.0, format="%.2f"),
            },
        )
        st.download_button("⬇️ Pobierz JSON (kontrakt dla kolejnych agentów)",
                           data=ScoutReport(run_id="export", started_at=pd.Timestamp.now().to_pydatetime(),
                                            model="-", reference_date=date.today(), events=events).to_json_events(),
                           file_name="torowania.json", mime="application/json")

        st.subheader("Szczegóły wydarzenia")
        idx = st.selectbox("Wybierz termin", range(len(events)),
                           format_func=lambda i: f"{events[i].date_start} · {events[i].track} · {events[i].organizer}")
        e = events[idx]
        m1, m2, m3, m4 = st.columns(4)
        m1.metric("Cena", f"{e.price:g} {e.currency}" if e.price is not None else "brak")
        m2.metric("Wolne miejsca", e.seats_left if e.seats_left is not None else (e.availability_note or "?"))
        m3.metric("Pewność ekstrakcji", f"{e.confidence:.0%}")
        m4.metric("Pogoda", "—", help="Agent 3 (Meteorolog) - kolejny etap")
        st.markdown(f"**{e.name}** · [{e.url}]({e.url})")
        if e.price_note:
            st.caption(f"Cena: {e.price_note}")
        if e.issues:
            st.warning("Uwagi walidacji: " + "; ".join(e.issues))

with tab_map:
    pts = pd.DataFrame([{"lat": e.lat, "lon": e.lon, "tor": e.track} for e in events if e.lat is not None])
    if pts.empty:
        st.info("Brak torów ze współrzędnymi.")
    else:
        st.map(pts.drop_duplicates(), size=4000)
        st.caption("Współrzędne z ręcznej listy torów (data/tracks.json). Odległość od użytkownika policzy Agent 2.")

with tab_report:
    if report is None:
        st.info("Agent nie był jeszcze uruchamiany.")
    else:
        st.write(f"Przebieg **{report.run_id}** · model `{report.model}` · data referencyjna {report.reference_date} · "
                 f"tokeny: {report.input_tokens} wej. / {report.output_tokens} wyj. · "
                 f"usunięte duplikaty: {report.duplicates_removed}")
        if report.sources:
            st.dataframe(pd.DataFrame([{
                "Źródło": s.source_name, "OK": "✅" if s.ok else "❌", "Błąd": s.error or "",
                "Fragmenty": s.chunks, "Wyodrębnione": s.raw_events, "Poprawne": s.valid_events,
                "Odrzucone": s.rejected_events, "Podejrzana treść": "⚠️" if s.suspicious_content else "",
            } for s in report.sources]), hide_index=True, use_container_width=True)
        for w in report.warnings:
            st.warning(w)
        with st.expander("Ślad działania agenta (kroki i narzędzia)"):
            for step in report.steps:
                st.write(("✅ " if step.ok else "⚠️ ") + f"`{step.timestamp:%H:%M:%S}` **{step.tool}** - {step.detail}")

with tab_agent:
    st.markdown("""
**Nazwa:** Poszukiwacz Terminów (Event Scout) · **Rola:** zbieranie informacji o torowaniach ze stron organizatorów.

**Dane wejściowe:** lista źródeł (strony organizatorów / własne adresy / wklejony tekst) i data referencyjna.
**Wynik:** ustrukturyzowana lista wydarzeń (JSON) zapisana w SQLite.
**Komunikacja z modelem:** Anthropic Messages API, *structured outputs* - odpowiedź walidowana schematem Pydantic.

**Narzędzia:** `fetch_source` → `html_to_text` → `detect_injection` → `llm_extract` → `validate` → `deduplicate` → `sqlite`
""")
    with st.expander("Instrukcja systemowa agenta"):
        st.code(EVENT_SCOUT_SYSTEM, language="markdown")
    with st.expander("Schemat wyniku zwracanego przez model (JSON Schema)"):
        st.json(ExtractionResult.model_json_schema())

with tab_next:
    st.info("Watchlista, wykres liczby miejsc w czasie, powiadomienia e-mail, pogoda, rekomendacje "
            "„kup teraz / poczekaj” i Kalendarz Google pojawią się wraz z kolejnymi agentami "
            "(Nawigator, Meteorolog, Strażnik Miejsc, Doradca). Baza już zapisuje historię liczby miejsc.")

# czat - pytanie w języku naturalnym obsłuży Agent 5 (Doradca)
st.chat_input("Zapytaj, np. „Co jest w czerwcu do 300 km od Warszawy do 600 zł?” (Doradca - kolejny etap)", disabled=True)
