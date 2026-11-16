"""TrackDayAgents - aplikacja Streamlit (etap 1: Agent 1 - Poszukiwacz Terminów).

Uruchomienie:  streamlit run app.py
"""

from __future__ import annotations

import hashlib
from datetime import date, timedelta

import pandas as pd
import streamlit as st

from trackday.agents.event_scout import EventScoutAgent, ScoutInput
from trackday.agents.prompts import EVENT_SCOUT_SYSTEM
from trackday.config import CLAUDE_MODELS, OLLAMA_PREFIX, get_settings
from trackday.evaluation import golden_as_events
from trackday.llm import create_llm, is_local, list_ollama_models
from trackday.models import ExtractionResult, ScoutReport, TrackDayEvent
from trackday.sources import SourceError, load_sources, make_source
from trackday.tools.normalize import (GMAPS_COLUMN, TRACK_COLUMNS, build_tracks, default_track_defs, google_maps_url,
                                      match_track, tracks_from_rows)
from trackday.views import all_groups, group_by_day
from trackday.storage.db import EventRepository

st.set_page_config(page_title="TrackDay Agents", page_icon="🏍️", layout="wide")

settings = get_settings()


@st.cache_resource
def get_repo(path: str) -> EventRepository:
    repo = EventRepository(path)
    repo.init_sources(load_sources())  # tylko przy pierwszym uruchomieniu
    repo.init_tracks(default_track_defs())
    return repo


@st.cache_data(ttl=60, show_spinner=False)
def model_options(ollama_url: str) -> dict[str, str]:
    """Modele lokalne (Ollama, darmowe) + modele Claude (płatne API)."""
    local = {f"{OLLAMA_PREFIX}{m}": f"{m} (lokalnie, za darmo)" for m in list_ollama_models(ollama_url)}
    return local | CLAUDE_MODELS


repo = get_repo(str(settings.db_path))

# ============================================================== SIDEBAR
with st.sidebar:
    st.title("🏍️ TrackDay Agents")
    st.caption("Agent 1 - Poszukiwacz Terminów")

    active_sources = [s for s in repo.list_sources() if s.enabled]
    # Formularz: zmiany pól nie uruchamiają agenta - dopiero przycisk lub Ctrl+Enter w polu tekstowym.
    with st.form("scout"):
        st.subheader("1. Źródła")
        if active_sources:
            st.markdown(f"Aktywne źródła ({len(active_sources)}):\n"
                        + "\n".join(f"- {s.name}" for s in active_sources))
        else:
            st.markdown("Brak aktywnych źródeł.")
        st.caption("Listę stron edytujesz w zakładce **Źródła**.")
        pasted = st.text_area("Dodatkowo: wklej treść ogłoszenia / posta (jednorazowo)", height=120,
                              placeholder="np. treść posta z Facebooka z terminem torowania")

        st.subheader("2. Model")
        models = model_options(settings.ollama_url)
        model_ids = list(models)
        model = st.selectbox("Model językowy", model_ids, format_func=models.get,
                             index=model_ids.index(settings.model) if settings.model in model_ids else 0,
                             help="Modele lokalne działają przez Ollamę: bez klucza i bez opłat. "
                                  "Modele Claude wymagają klucza API (płatne za tokeny).")
        if not any(is_local(m) for m in model_ids):
            st.caption("Nie wykryto Ollamy - uruchom aplikację Ollama, by używać modeli lokalnych.")
        api_key = settings.api_key or st.text_input("ANTHROPIC_API_KEY (tylko dla modeli Claude)", type="password",
                                                    help="Klucz nie jest zapisywany. Najlepiej ustaw go w pliku .env.")
        ref_date = st.date_input("Data referencyjna", value=date.today(),
                                 help="Względem niej agent uzupełnia brakujący rok w datach.")

        run_clicked = st.form_submit_button("Szukaj terminów (uruchom agenta)", type="primary",
                                            icon=":material/search:", width="stretch")
        st.caption("Ctrl+Enter w polu tekstowym też uruchamia wyszukiwanie.")

    demo_clicked = st.button("Tryb demo: wczytaj zestaw wzorcowy (bez LLM)", width="stretch")

    st.subheader("3. Filtry")
    today = date.today()
    d_range = st.date_input("Zakres dat", value=(today, today + timedelta(days=365)))
    max_price = st.slider("Maksymalny koszt [zł]", 0, 2000, 2000, step=50)
    track_filter = st.multiselect("Tory", repo.distinct("track"))
    org_filter = st.multiselect("Organizatorzy", repo.distinct("organizer"))
    group_filter = st.multiselect("Grupy / poziom", all_groups(repo.list_events()),
                                  help="Pokazuje tylko wybory (sesja × grupa) z zaznaczonych grup.")
    only_available = st.checkbox("Tylko z wolnymi miejscami")
    if st.button("Wyczyść zebrane terminy", width="stretch", help="Lista źródeł zostaje bez zmian."):
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
    srcs = active_sources
    if not srcs and not pasted.strip():
        msg = "Brak aktywnych źródeł - dodaj stronę w zakładce Źródła lub wklej treść."
        st.error(msg, icon=":material/error:")
        st.toast(msg, icon=":material/error:")
    elif not api_key and not is_local(model):
        msg = ("Agent nie wystartował: brak klucza ANTHROPIC_API_KEY. Wpisz go w pole „ANTHROPIC_API_KEY” "
               "w panelu bocznym albo zapisz w pliku .env (wzór: .env.example) i uruchom aplikację ponownie. "
               "Możesz też wybrać model lokalny (Ollama) - działa za darmo, bez klucza.")
        st.error(msg, icon=":material/key_off:")
        st.toast("Brak klucza ANTHROPIC_API_KEY", icon=":material/key_off:")
    else:
        agent = EventScoutAgent(create_llm(model, settings, api_key=api_key), settings=settings, repository=repo)
        label = "Agent przeszukuje źródła…" + (" (model lokalny - może to potrwać kilka minut)" if is_local(model) else "")
        with st.status(label, expanded=True) as status:
            st.write(f"Źródła: {', '.join(s.name for s in srcs) or '-'}" + (" + wklejony tekst" if pasted.strip() else ""))
            report = agent.run(ScoutInput(sources=srcs, pasted_text=pasted or None, reference_date=ref_date))
            for step in report.steps:
                st.write(("✅ " if step.ok else "⚠️ ") + f"**{step.tool}** - {step.detail}")
            status.update(label=f"Gotowe: {len(report.events)} wydarzeń", state="complete")
        st.session_state["report"] = report

report: ScoutReport | None = st.session_state.get("report") or repo.last_run()


# ============================================================== WIDOK
st.title("Torowania motocyklowe")
st.caption("Jedna lista terminów od wielu organizatorów - dane zebrane przez agenta „Poszukiwacz Terminów”.")

d_from, d_to = (d_range if isinstance(d_range, tuple) and len(d_range) == 2 else (None, None))
events = repo.list_events(date_from=d_from, date_to=d_to, max_price_pln=None if max_price >= 2000 else max_price,
                          tracks=track_filter or None, organizers=org_filter or None, only_available=only_available)
days = group_by_day(events, set(group_filter) or None, only_available)
events = [e for d in days for e in d.events]  # po filtrze grup

tab_events, tab_map, tab_sources, tab_tracks, tab_report, tab_agent, tab_next = st.tabs(
    ["📅 Terminy", "🗺️ Mapa", "🌐 Źródła", "🏁 Tory", "🧾 Raport agenta", "🤖 O agencie",
     "🔜 Obserwowane / Powiadomienia"])

with tab_sources:
    st.subheader("Stała lista stron przeszukiwanych przez agenta")
    st.caption("Agent przy każdym uruchomieniu przeszukuje wszystkie **aktywne** strony z tej listy. "
               "Dodaj wiersz (+ pod tabelą), wpisz adres strony z terminami i kliknij **Zapisz listę**. "
               "Wiersz usuwasz, zaznaczając go i naciskając Delete (ikona kosza).")
    st.caption("**Podstrony:** jeśli strona to tylko lista wydarzeń, a szczegóły są po kliknięciu, wpisz w kolumnie "
               "*Wzorzec linków do podstron* fragment adresu podstron wydarzeń (np. `track-day`) albo wyrażenie "
               "regularne (np. `\\d{1,2}-\\d{1,2}-\\d{2}/?$` = adres kończący się datą). Agent wejdzie w każdą "
               "pasującą podstronę (max *Limit podstron*) i odczyta z niej szczegóły. Puste = analiza samej strony.")
    saved = repo.list_sources()
    sources_df = pd.DataFrame(
        [{"Aktywne": s.enabled, "Nazwa": s.name, "Organizator": s.organizer or "", "Adres strony": s.url,
          "Wzorzec linków do podstron": s.detail_link_pattern or "", "Limit podstron": s.max_detail_pages}
         for s in saved],
        columns=["Aktywne", "Nazwa", "Organizator", "Adres strony", "Wzorzec linków do podstron", "Limit podstron"],
    ).astype({"Aktywne": bool, "Nazwa": str, "Organizator": str, "Adres strony": str,
              "Wzorzec linków do podstron": str, "Limit podstron": "Int64"})
    edited = st.data_editor(
        sources_df,
        key=f"sources_editor_{st.session_state.get('sources_version', 0)}",
        num_rows="dynamic",
        hide_index=True,
        column_config={
            "Aktywne": st.column_config.CheckboxColumn(default=True, width="small"),
            "Nazwa": st.column_config.TextColumn(help="Dowolna nazwa; puste = nazwa domeny"),
            "Organizator": st.column_config.TextColumn(help="Organizator torowań z tej strony (opcjonalnie)"),
            "Adres strony": st.column_config.TextColumn(required=True, help="https://... lub sample://plik.html",
                                                        width="large"),
            "Wzorzec linków do podstron": st.column_config.TextColumn(
                help="Opcjonalnie. Fragment adresu albo wyrażenie regularne - agent wejdzie w każdą podstronę, "
                     "której adres pasuje, i odczyta z niej szczegóły wydarzenia."),
            "Limit podstron": st.column_config.NumberColumn(
                min_value=1, max_value=100, step=1, default=20, width="small",
                help="Maksymalna liczba podstron odczytywanych z tej strony (każda to osobne zapytanie do modelu)."),
        },
        alt="Lista stron organizatorów przeszukiwanych przez agenta",
    )
    if st.button("Zapisz listę", type="primary", icon=":material/save:"):
        old_by_url = {s.url: s for s in saved}
        new_sources, errors, seen = [], [], set()
        def cell(v) -> str:
            return "" if v is None or pd.isna(v) else str(v).strip()

        for i, row in enumerate(edited.to_dict("records"), start=1):
            url = cell(row.get("Adres strony"))
            if not url:
                continue
            if url in seen:
                errors.append(f"Wiersz {i}: adres {url} jest już na liście.")
                continue
            seen.add(url)
            try:
                prev = old_by_url.get(url)
                new_sources.append(make_source(
                    url, name=cell(row.get("Nazwa")), organizer=cell(row.get("Organizator")),
                    enabled=True if pd.isna(row.get("Aktywne")) else bool(row.get("Aktywne")),
                    base_url=prev.base_url if prev else None,
                    detail_link_pattern=cell(row.get("Wzorzec linków do podstron")),
                    max_detail_pages=None if pd.isna(row.get("Limit podstron")) else int(row.get("Limit podstron"))))
            except SourceError as e:
                errors.append(f"Wiersz {i}: {e}")
        if errors:
            for err in errors:
                st.error(err)
            st.warning("Nie zapisano zmian - popraw błędy i spróbuj ponownie.")
        else:
            repo.save_sources(new_sources)
            st.session_state["sources_version"] = st.session_state.get("sources_version", 0) + 1
            st.toast(f"Zapisano listę źródeł ({len(new_sources)}).")
            st.rerun()

with tab_tracks:
    st.subheader("Znane tory")
    st.caption("Agent dopasowuje nazwę toru z ogłoszenia do tej listy (także po *innych nazwach*) i bierze stąd "
               "współrzędne do mapy. Dodaj wiersz (+ pod tabelą), wpisz nazwę i współrzędne - najprościej kliknąć "
               "prawym przyciskiem tor w Mapach Google i skopiować liczby, np. `52.4176, 16.8089`. "
               "Po zapisaniu zebrane już terminy są dopasowywane ponownie.")
    saved_tracks = repo.list_tracks()
    known = build_tracks(saved_tracks)
    missing = sorted({e.track for e in repo.list_events() if match_track(e.track, known) is None})
    if missing:
        st.info("Tory z zebranych terminów, których nie ma na liście (brak na mapie): "
                + ", ".join(f"**{m}** ([szukaj w Mapach Google ↗]({google_maps_url(query=m)}))" for m in missing)
                + ". Dodaj je poniżej.", icon=":material/location_off:")
    tracks_df = pd.DataFrame(
        [dict(zip(TRACK_COLUMNS + [GMAPS_COLUMN], (t.name, t.country or "", f"{t.lat:.5f}, {t.lon:.5f}",
                                                    ", ".join(t.aliases), google_maps_url(t.lat, t.lon))))
         for t in saved_tracks],
        columns=TRACK_COLUMNS + [GMAPS_COLUMN],
    ).astype(str)
    edited_tracks = st.data_editor(
        tracks_df,
        key=f"tracks_editor_{st.session_state.get('tracks_version', 0)}",
        num_rows="dynamic", hide_index=True,
        column_config={
            "Nazwa": st.column_config.TextColumn(required=True, help="Nazwa wyświetlana w terminach i na mapie"),
            "Kraj": st.column_config.TextColumn(width="small", help="Kod kraju, np. PL, CZ (opcjonalnie)"),
            "Współrzędne (szer., dł.)": st.column_config.TextColumn(
                required=True, help="Szerokość i długość geograficzna, np. 52.4176, 16.8089 (format z Map Google)"),
            "Inne nazwy (po przecinku)": st.column_config.TextColumn(
                width="large", help="Nazwy używane przez organizatorów, np. „Poznań, Tor Poznań, Przeźmierowo”"),
            GMAPS_COLUMN: st.column_config.LinkColumn(
                display_text="otwórz ↗", disabled=True,
                help="Otwiera tor w Mapach Google (nowe tory - po zapisaniu)"),
        },
        alt="Lista znanych torów z współrzędnymi",
    )
    if st.button("Zapisz tory", type="primary", icon=":material/save:"):
        new_tracks, t_errors = tracks_from_rows(edited_tracks.to_dict("records"))
        if t_errors:
            for err in t_errors:
                st.error(err)
            st.warning("Nie zapisano zmian - popraw błędy i spróbuj ponownie.")
        else:
            repo.save_tracks(new_tracks)
            n = repo.relink_tracks(build_tracks(new_tracks))
            st.session_state["tracks_version"] = st.session_state.get("tracks_version", 0) + 1
            st.toast(f"Zapisano tory ({len(new_tracks)}); zaktualizowano terminy: {n}.")
            st.rerun()

with tab_events:
    c1, c2, c3, c4 = st.columns(4)
    c1.metric("Dni z terminami", len(days))
    c2.metric("Wybory (sesja × grupa)", sum(len(d.rows) for d in days))
    c3.metric("Organizatorzy", len({e.organizer for e in events}))
    low = [r for d in days for r in d.rows if isinstance(r["Wolne miejsca"], int) and 0 < r["Wolne miejsca"] < 3]
    c4.metric("Prawie wyprzedane (<3 miejsca)", len(low))
    if not events:
        st.info("Brak terminów. Sprawdź listę stron w zakładce Źródła i kliknij **Szukaj terminów** "
                "(albo wczytaj tryb demo). Jeśli ustawiono filtr grup, żaden wybór do niego nie pasuje.")
    else:
        st.caption("Jeden wiersz na dzień i tor - rozwiń, aby zobaczyć każdy wybór (wydarzenie, godzina, sesja, "
                   "grupa) z liczbą wolnych miejsc.")
        for d in days:
            with st.expander(d.label(), key=f"day_{d.day:%Y%m%d}_{hashlib.sha1(d.track.encode()).hexdigest()[:8]}"):
                st.dataframe(
                    pd.DataFrame(d.rows).astype({"Wolne miejsca": str}), width="stretch", hide_index=True,
                    column_order=["Godzina", "Wydarzenie", "Nitka / odmiana", "Grupa", "Wolne miejsca", "Cena",
                                  "Sesja", "Organizator", "Link", "Uwagi"],
                    column_config={
                        "Link": st.column_config.LinkColumn("Link", display_text="otwórz ↗"),
                        "Cena [zł]": None,
                        "Organizator": None if len(d.organizers) == 1 else st.column_config.TextColumn(),
                        "Wolne miejsca": st.column_config.TextColumn(),
                    },
                    alt=f"Wybory dnia {d.day:%d.%m.%Y} na torze {d.track}",
                )
        st.download_button("⬇️ Pobierz JSON (kontrakt dla kolejnych agentów)",
                           data=ScoutReport(run_id="export", started_at=pd.Timestamp.now().to_pydatetime(),
                                            model="-", reference_date=date.today(), events=events).to_json_events(),
                           file_name="torowania.json", mime="application/json")

        st.subheader("Szczegóły wydarzenia")
        idx = st.selectbox("Wybierz termin", range(len(events)),
                           format_func=lambda i: " · ".join(x for x in (str(events[i].date_start), events[i].start_time, events[i].track,
                                                                  events[i].name, events[i].organizer) if x))
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
        st.caption("Współrzędne z listy torów (zakładka „Tory”). Odległość od użytkownika policzy Agent 2.")

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
                "Podstrony": s.detail_pages, "Fragmenty": s.chunks, "Wyodrębnione": s.raw_events, "Poprawne": s.valid_events,
                "Odrzucone": s.rejected_events, "Podejrzana treść": "⚠️" if s.suspicious_content else "",
            } for s in report.sources]), hide_index=True, width="stretch")
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
