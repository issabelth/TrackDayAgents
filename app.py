"""TrackDayAgents - aplikacja Streamlit (etap 1: Agent 1 - Poszukiwacz Terminów).

Uruchomienie:  streamlit run app.py
"""

from __future__ import annotations

from datetime import date, timedelta

import pandas as pd
import streamlit as st

from trackday.agents.event_scout import EventScoutAgent, ScoutInput
from trackday.agents.event_scout import _parse_time as parse_time
from trackday.agents.prompts import EVENT_SCOUT_SYSTEM
from trackday.config import CLAUDE_MODELS, OLLAMA_PREFIX, get_settings
from trackday.evaluation import golden_as_events
from trackday.llm import create_llm, is_local, list_ollama_models, supports_vision
from trackday.models import ExtractionResult, ScoutReport, TrackDayEvent
from trackday.sources import SourceError, is_facebook_url, load_sources, make_source
from trackday.tools.images import MAX_IMAGES, ImageError, prepare_image
from trackday.tools.normalize import (COL_ALIASES, COL_COORDS, COL_COUNTRY, COL_LINK, COL_NAME, GMAPS_COLUMN,
                                      TRACK_COLUMNS, build_tracks, default_track_defs, google_maps_url, match_track,
                                      event_id, track_row, tracks_from_rows)
from trackday.views import (MONTHS, WEEKDAYS, DayGroup, all_groups, by_date, chip_label, day_key, group_by_day,
                            month_weeks, overlaps, period_title, shift_month, track_colors, week_of)
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

    def load_demo(reference_date: date) -> None:
        events = golden_as_events()
        repo.save_run(ScoutReport(run_id="demo", started_at=pd.Timestamp.now().to_pydatetime(),
                                  model="demo (bez LLM)", reference_date=reference_date, events=events))
        st.session_state["report"] = repo.last_run()
        st.toast(f"Wczytano {len(events)} wydarzeń demonstracyjnych.")

    st.button("Tryb demo: wczytaj zestaw wzorcowy (bez LLM)", width="stretch", on_click=load_demo, args=(ref_date,))

    st.subheader("3. Filtry")
    today = date.today()
    FILTER_DEFAULTS = {
        "f_range": (today, today + timedelta(days=365)), "f_weekdays": [], "f_price": (0, 2000),
        "f_tracks": [], "f_orgs": [], "f_groups": [], "f_avail": False, "f_hide": False,
    }
    for k, v in FILTER_DEFAULTS.items():
        st.session_state.setdefault(k, v)  # wartości domyślne w jednym miejscu (używa ich też „Wyczyść filtry”)
    d_range = st.date_input("Zakres dat", key="f_range")
    weekdays = st.pills("Dni tygodnia", list(range(7)), selection_mode="multi", key="f_weekdays",
                        format_func=lambda d: WEEKDAYS[d],
                        help="Np. tylko sobota i niedziela. Nic nie zaznaczone = wszystkie dni.")
    min_price, max_price = st.slider("Koszt [zł]", 0, 2000, step=50, key="f_price",
                                     help="Terminy bez podanej ceny są pokazywane zawsze.")
    track_filter = st.multiselect("Tory", repo.distinct("track"), key="f_tracks")
    org_filter = st.multiselect("Organizatorzy", repo.distinct("organizer"), key="f_orgs")
    group_filter = st.multiselect("Grupy / poziom", all_groups(repo.list_events()), key="f_groups",
                                  help="Pokazuje tylko wybory (sesja × grupa) z zaznaczonych grup.")
    only_available = st.checkbox("Tylko z wolnymi miejscami", key="f_avail")
    hide_missing = st.checkbox("Ukryj terminy, których nie ma już na stronie organizatora", key="f_hide",
                               help="Terminy są zapisywane na stałe. Jeśli kolejne wyszukiwanie nie znajdzie "
                                    "terminu na stronie (odwołany, zdjęty), zostaje on w bazie z adnotacją.")
    def clear_events() -> None:
        repo.clear()
        st.session_state.pop("report", None)
        st.toast("Usunięto zebrane terminy.")

    st.button("Wyczyść zebrane terminy", width="stretch", help="Lista źródeł i torów zostaje bez zmian.",
              on_click=clear_events)

# ============================================================== AKCJE
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
                          min_price_pln=min_price or None, weekdays=weekdays or None,
                          tracks=track_filter or None, organizers=org_filter or None, only_available=only_available,
                          hide_missing=hide_missing)
n_saved = len(repo.list_events())
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
    # Zapis w funkcji wywoływanej przy kliknięciu (on_click) - działa przed odświeżeniem strony, więc nie trzeba
    # st.rerun() (ten przerywa skrypt wyjątkiem, na którym zatrzymuje się debugger VS).
    def save_sources_cb(edited: pd.DataFrame, saved: list) -> None:
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
            st.session_state["sources_errors"] = errors
        else:
            repo.save_sources(new_sources)
            st.session_state["sources_version"] = st.session_state.get("sources_version", 0) + 1
            st.toast(f"Zapisano listę źródeł ({len(new_sources)}).")

    st.button("Zapisz listę", type="primary", icon=":material/save:", on_click=save_sources_cb, args=(edited, saved))
    if errs := st.session_state.pop("sources_errors", None):
        for err in errs:
            st.error(err)
        st.warning("Nie zapisano zmian - popraw błędy i spróbuj ponownie.")

    # ---------------------------------------------------------- terminy dodawane ręcznie
    st.divider()
    st.subheader("Dodaj terminy ręcznie")
    st.caption("Dla organizatorów, którzy publikują terminy na Facebooku: kalendarz sezonu jako zdjęcie w poście "
               "albo wydarzenie przed konkretnym torowaniem. Facebook nie pozwala agentowi pobierać swoich stron, "
               "więc wgraj **zrzut ekranu** albo wklej **opis wydarzenia** - agent odczyta terminy, a Ty je "
               "przejrzysz i zatwierdzisz przed zapisem. Działa też dla zwykłych stron.")
    http_sources = sorted([x for x in saved if x.url.startswith(("http://", "https://"))],
                          key=lambda x: (not is_facebook_url(x.url), x.name.lower()))
    track_names = [t.name for t in repo.list_tracks()]
    AUTO_TRACK = "— odczytaj z treści —"
    vision = {m: supports_vision(m, settings.ollama_url) for m in model_ids}
    default_vision = model if vision.get(model) else next(
        (m for m in model_ids if vision[m] and is_local(m) and "gemma4" in m),
        next((m for m in model_ids if vision[m] and is_local(m)), model_ids[0] if model_ids else None))

    def save_manual(src_id: str, rep: ScoutReport, edited: pd.DataFrame) -> None:
        keep = []
        for ev, row in zip(rep.events, edited.to_dict("records")):
            if not row.get("Zapisz"):
                continue
            day = pd.Timestamp(row["Data"]).date() if pd.notna(row.get("Data")) else ev.date_start
            end = pd.Timestamp(row["Do dnia"]).date() if pd.notna(row.get("Do dnia")) else None
            hour = parse_time(str(row.get("Godzina") or "")) or None
            layout = (str(row.get("Odmiana") or "").strip()[:60]) or None
            name = str(row.get("Nazwa") or "").strip() or ev.name
            keep.append(ev.model_copy(update={
                "date_start": day, "date_end": end if end and end > day else None,
                "start_time": hour, "track_layout": layout, "name": name,
                "id": event_id(ev.organizer, ev.track, day, extra=f"manual {hour or ''} {layout or ''}".strip())}))
        res = repo.upsert_events(keep)
        st.session_state.pop(f"manual_result_{src_id}", None)
        st.session_state[f"manual_ver_{src_id}"] = st.session_state.get(f"manual_ver_{src_id}", 0) + 1
        st.toast(f"Zapisano terminy: {res.added} nowych, {res.updated} zaktualizowanych - widać je w zakładce "
                 "Terminy.")

    if not http_sources:
        st.info("Najpierw dodaj stronę organizatora do listy powyżej (np. adres jego wydarzeń na Facebooku).")
    for src in http_sources:
        fb = is_facebook_url(src.url)
        ver = st.session_state.get(f"manual_ver_{src.id}", 0)
        with st.expander(f"{'📘' if fb else '🌐'} Dodaj terminy - **{src.name}**" + (" (Facebook)" if fb else ""),
                         key=f"manual_exp_{src.id}"):
            if fb:
                st.link_button("Otwórz wydarzenia na Facebooku", src.url, icon=":material/open_in_new:")
                st.caption("Kalendarz sezonu: zrób zrzut ekranu zdjęcia z posta (Win+Shift+S). Wydarzenie: zrzut "
                           "ekranu strony wydarzenia albo skopiowany opis (data, godzina, cena, grupy).")
            c1, c2 = st.columns(2)
            m_track = c1.selectbox("Tor, którego dotyczą terminy", [AUTO_TRACK] + track_names,
                                   key=f"manual_track_{src.id}",
                                   help="Wybrany tor zostanie przypisany wszystkim odczytanym terminom. Brakuje "
                                        "toru? Dodaj go w zakładce Tory.")
            m_model = c2.selectbox(
                "Model", model_ids, key=f"manual_model_{src.id}",
                index=model_ids.index(default_vision) if default_vision in model_ids else 0,
                format_func=lambda m: ("📷 " if vision.get(m) else "") + models.get(m, m),
                help="📷 = model czyta obrazy (potrzebne do zrzutów ekranu). Lokalnie: gemma4 / gemma3.")
            files = st.file_uploader("Zrzuty ekranu (PNG / JPG)", type=["png", "jpg", "jpeg", "webp"],
                                     accept_multiple_files=True, key=f"manual_files_{src.id}_{ver}")
            m_text = st.text_area("…i/lub wklej opis wydarzenia / treść posta", key=f"manual_text_{src.id}_{ver}",
                                  height=100)
            if st.button("Odczytaj terminy", key=f"manual_read_{src.id}", icon=":material/document_scanner:",
                         type="primary"):
                images, img_errors = [], []
                for f in (files or [])[:MAX_IMAGES]:
                    try:
                        images.append(prepare_image(f.getvalue(), f.name))
                    except ImageError as err:
                        img_errors.append(str(err))
                if files and len(files) > MAX_IMAGES:
                    img_errors.append(f"Naraz można odczytać {MAX_IMAGES} zrzutów - pozostałe pominięto.")
                for err in img_errors:
                    st.error(err)
                if images and not vision.get(m_model):
                    st.error("Wybrany model nie czyta obrazów - wybierz model oznaczony 📷.")
                elif not images and not (m_text or "").strip():
                    st.warning("Dodaj zrzut ekranu albo wklej opis wydarzenia.")
                elif not is_local(m_model) and not api_key:
                    st.error("Model Claude wymaga klucza API - wybierz model lokalny albo podaj klucz.")
                else:
                    with st.spinner("Agent odczytuje terminy… (model lokalny: ok. 1 min na zrzut ekranu)"):
                        agent = EventScoutAgent(create_llm(m_model, settings, api_key), settings=settings,
                                                repository=repo)
                        st.session_state[f"manual_result_{src.id}"] = agent.import_manual(
                            src, images=images, text=m_text, reference_date=ref_date,
                            track=None if m_track == AUTO_TRACK else m_track)

            rep = st.session_state.get(f"manual_result_{src.id}")
            if rep is not None:
                sr = rep.sources[0]
                if not sr.ok:
                    st.error(sr.error or "Nie udało się odczytać terminów.")
                for w in sr.warnings:
                    st.caption(f"⚠️ {w}")
                if sr.suspicious_content:
                    st.warning("Treść zawiera podejrzane instrukcje (możliwy prompt injection) - sprawdź terminy "
                               "szczególnie uważnie.")
                if rep.events:
                    st.markdown(f"Odczytane terminy: **{len(rep.events)}** - popraw daty lub godziny, jeśli trzeba, "
                                "odznacz błędne i zapisz.")
                    preview = pd.DataFrame([{
                        "Zapisz": True, "Data": e.date_start, "Do dnia": e.date_end, "Godzina": e.start_time or "",
                        "Odmiana": e.track_layout or "", "Nazwa": e.name, "Tor": e.track,
                        "Uwagi": "; ".join(i for i in e.issues if i != "dodane ręcznie przez użytkownika"),
                    } for e in rep.events])
                    edited_manual = st.data_editor(
                        preview, key=f"manual_editor_{src.id}_{rep.run_id}", hide_index=True, width="stretch",
                        num_rows="fixed", disabled=["Tor", "Uwagi"],
                        column_config={
                            "Zapisz": st.column_config.CheckboxColumn(width="small"),
                            "Data": st.column_config.DateColumn(format="DD.MM.YYYY", required=True),
                            "Do dnia": st.column_config.DateColumn(
                                format="DD.MM.YYYY", help="Ostatni dzień wydarzenia kilkudniowego (puste = "
                                                          "jednodniowe)"),
                            "Godzina": st.column_config.TextColumn(width="small", help="np. 13:30 (opcjonalnie)"),
                            "Odmiana": st.column_config.TextColumn(width="small", help="np. REVERS"),
                        },
                    )
                    st.button("Zapisz zaznaczone terminy", key=f"manual_save_{src.id}", type="primary",
                              icon=":material/save:", on_click=save_manual, args=(src.id, rep, edited_manual))
                elif sr.ok:
                    st.info("Model nie znalazł żadnych terminów w tych materiałach.")


with tab_tracks:
    st.subheader("Znane tory")
    st.caption("Agent dopasowuje nazwę toru z ogłoszenia do tej listy (także po *innych nazwach*) i bierze stąd "
               "współrzędne do mapy. Dodaj wiersz (+ pod tabelą), wpisz nazwę i **wklej link do toru z Map Google** "
               "(przycisk „Udostępnij” → „Kopiuj link” albo adres z paska przeglądarki) - współrzędne zostaną "
               "odczytane z linku. Można też wpisać współrzędne ręcznie: kliknij tor prawym przyciskiem w Mapach "
               "Google i skopiuj liczby, np. `52.4176, 16.8089`. "
               "Po zapisaniu zebrane już terminy są dopasowywane ponownie.")
    saved_tracks = repo.list_tracks()
    known = build_tracks(saved_tracks)
    missing = sorted({e.track for e in repo.list_events() if match_track(e.track, known) is None})
    if missing:
        st.info("Tory z zebranych terminów, których nie ma na liście (brak na mapie): "
                + ", ".join(f"**{m}** ([szukaj w Mapach Google ↗]({google_maps_url(query=m)}))" for m in missing)
                + ". Dodaj je poniżej.", icon=":material/location_off:")
    tracks_df = pd.DataFrame(
        [track_row(t) for t in saved_tracks],
        columns=TRACK_COLUMNS + [GMAPS_COLUMN],
    ).astype(str)
    edited_tracks = st.data_editor(
        tracks_df,
        key=f"tracks_editor_{st.session_state.get('tracks_version', 0)}",
        num_rows="dynamic", hide_index=True,
        column_config={
            COL_NAME: st.column_config.TextColumn(required=True, help="Nazwa wyświetlana w terminach i na mapie"),
            COL_COUNTRY: st.column_config.TextColumn(width="small", help="Kod kraju, np. PL, CZ (opcjonalnie)"),
            COL_LINK: st.column_config.TextColumn(
                help="Link do toru w Mapach Google, np. https://maps.app.goo.gl/… - współrzędne zostaną odczytane "
                     "z linku przy zapisie (wtedy kolumnę współrzędnych można zostawić pustą)"),
            COL_COORDS: st.column_config.TextColumn(
                help="Szerokość i długość geograficzna, np. 52.4176, 16.8089 (format z Map Google). "
                     "Puste, jeśli podano link - wypełnią się po zapisaniu"),
            COL_ALIASES: st.column_config.TextColumn(
                width="large", help="Nazwy używane przez organizatorów, np. „Poznań, Tor Poznań, Przeźmierowo”"),
            GMAPS_COLUMN: st.column_config.LinkColumn(
                display_text="otwórz ↗", disabled=True,
                help="Otwiera tor w Mapach Google (nowe tory - po zapisaniu)"),
        },
        alt="Lista znanych torów z współrzędnymi",
    )
    def save_tracks_cb(edited: pd.DataFrame, saved: list) -> None:
        new_tracks, t_errors = tracks_from_rows(
            edited.to_dict("records"), known_links={t.maps_url: (t.lat, t.lon) for t in saved if t.maps_url})
        if t_errors:
            st.session_state["tracks_errors"] = t_errors
            return
        repo.save_tracks(new_tracks)
        n = repo.relink_tracks(build_tracks(new_tracks))
        st.session_state["tracks_version"] = st.session_state.get("tracks_version", 0) + 1
        st.toast(f"Zapisano tory ({len(new_tracks)}); zaktualizowano terminy: {n}.")

    st.button("Zapisz tory", type="primary", icon=":material/save:", on_click=save_tracks_cb,
              args=(edited_tracks, saved_tracks))
    if t_errs := st.session_state.pop("tracks_errors", None):
        for err in t_errs:
            st.error(err)
        st.warning("Nie zapisano zmian - popraw błędy i spróbuj ponownie.")

def reset_filters() -> None:
    """Przywraca domyślne wartości filtrów z panelu bocznego (ustawione jawnie, żeby odświeżyły się też widżety)."""
    st.session_state.update(FILTER_DEFAULTS)


def active_filters() -> list[str]:
    out = []
    if d_from and (d_from, d_to) != FILTER_DEFAULTS["f_range"]:
        out.append(f"{d_from:%d.%m.%Y} - {d_to:%d.%m.%Y}")
    if weekdays:
        out.append(", ".join(WEEKDAYS[d] for d in sorted(weekdays)))
    if (min_price, max_price) != (0, 2000):
        out.append(f"{min_price}-{max_price} zł")
    out += track_filter + org_filter + group_filter
    if only_available:
        out.append("z wolnymi miejscami")
    if hide_missing:
        out.append("bez zdjętych ze strony")
    return out


with tab_events:
    with st.container(horizontal=True, vertical_alignment="center"):
        if active := active_filters():
            st.markdown(":material/filter_alt: **Filtry:** " + " ".join(f":blue-badge[{f}]" for f in active))
            st.button("Wyczyść filtry", icon=":material/filter_alt_off:", type="tertiary", on_click=reset_filters)
        else:
            st.caption(":material/filter_alt: Filtry (panel boczny) dotyczą kalendarza i listy - teraz pokazane są "
                       "wszystkie terminy z najbliższego roku.")
    c1, c2, c3, c4 = st.columns(4)
    c1.metric("Dni z terminami", len({day for d in days for day in d.dates}))
    c2.metric("Wybory (sesja × grupa)", sum(len(d.rows) for d in days))
    c3.metric("Organizatorzy", len({e.organizer for e in events}))
    low = [r for d in days for r in d.rows if isinstance(r["Wolne miejsca"], int) and 0 < r["Wolne miejsca"] < 3]
    c4.metric("Prawie wyprzedane (<3 miejsca)", len(low))
    if not n_saved:
        st.info("Brak zapisanych terminów. Sprawdź listę stron w zakładce Źródła i kliknij **Szukaj terminów** "
                "(albo wczytaj tryb demo).")
    elif not events:
        st.info(f"Żaden z zapisanych terminów ({n_saved}) nie pasuje do filtrów - zmień je w panelu bocznym "
                "albo kliknij **Wyczyść filtry**.")
    else:
        colors = track_colors(days)
        on_date = by_date(days)

        def choices_table(d: DayGroup, compact: bool) -> None:
            cols = (["Godzina", "Grupa", "Wolne miejsca", "Cena"] if compact else
                    ["Godzina", "Wydarzenie", "Nitka / odmiana", "Grupa", "Wolne miejsca", "Cena", "Sesja",
                     "Organizator", "Link", "Uwagi"])
            st.dataframe(
                pd.DataFrame(d.rows).astype({"Wolne miejsca": str}), width="stretch", hide_index=True,
                column_order=cols,
                column_config={
                    "Link": st.column_config.LinkColumn("Link", display_text="otwórz ↗"),
                    "Cena [zł]": None,
                    "Organizator": None if len(d.organizers) == 1 else st.column_config.TextColumn(),
                    "Wolne miejsca": st.column_config.TextColumn(),
                },
                alt=f"Wybory dnia {d.day:%d.%m.%Y} na torze {d.track}",
            )

        def event_details(e: TrackDayEvent) -> None:
            st.markdown(f"**{e.name}**" + (f" · {e.start_time}" if e.start_time else "")
                        + (f" · {e.track_layout}" if e.track_layout else "") + f" · {e.organizer}")
            m1, m2, m3, m4 = st.columns(4)
            m1.metric("Cena", f"{e.price:g} {e.currency}" if e.price is not None else "brak")
            m2.metric("Wolne miejsca", e.seats_left if e.seats_left is not None else (e.availability_note or "?"))
            m3.metric("Pewność ekstrakcji", f"{e.confidence:.0%}")
            m4.metric("Pogoda", "—", help="Agent 3 (Meteorolog) - kolejny etap")
            st.link_button("Strona wydarzenia / zapisy", e.url, icon=":material/open_in_new:")
            if e.first_seen and e.last_seen:
                st.caption(f"W bazie od {e.first_seen:%d.%m.%Y %H:%M} · ostatnio potwierdzony na stronie "
                           f"{e.last_seen:%d.%m.%Y %H:%M}")
            if e.missing_since:
                st.warning(f"Ostatnie wyszukiwanie ({e.missing_since:%d.%m.%Y}) nie znalazło już tego terminu na "
                           "stronie organizatora - mógł zostać odwołany lub zdjęty.")
            if e.price_note:
                st.caption(f"Cena: {e.price_note}")
            if e.issues:
                st.warning("Uwagi walidacji: " + "; ".join(e.issues))

        @st.dialog("Szczegóły terminu", width="large")
        def day_dialog(d: DayGroup) -> None:
            st.markdown(f"### :{colors[d.track]}[●] {d.track}")
            st.caption(f"{d.date_label()} · {', '.join(d.organizers)}")
            choices_table(d, compact=False)
            for e in d.events:
                with st.container(border=True):
                    event_details(e)

        def open_details(key: str, card_key: str) -> None:
            st.session_state[card_key] = False  # zamknij kartę, zanim pokaże się okno
            st.session_state["cal_dialog"] = key

        def day_card(d: DayGroup, card_key: str) -> None:
            """Rozwinięty wpis (jak karta wydarzenia w Kalendarzu Google)."""
            with st.container(width=560):
                st.markdown(f"**:{colors[d.track]}[●] {d.track}**")
                st.caption(f"{d.date_label()} · {', '.join(d.organizers)}")
                st.markdown("\n".join(f"- {e.start_time + ' · ' if e.start_time else ''}{e.name}" for e in d.events))
                choices_table(d, compact=True)
                links = {e.url: e.start_time for e in d.events}
                with st.container(horizontal=True):
                    for url, time in links.items():
                        st.link_button(f"Zapisy {time}" if time and len(links) > 1 else "Zapisy", url,
                                       icon=":material/open_in_new:")
                    st.button("Pełne szczegóły", key=f"more_{card_key}", icon=":material/info:",
                              on_click=open_details, args=(day_key(d), card_key))

        # ---- pasek nawigacji (jak w Kalendarzu Google)
        if "cal_anchor" not in st.session_state:
            upcoming = [max(d.day, today) for d in days if d.last_day >= today]
            st.session_state["cal_anchor"] = upcoming[0] if upcoming else days[0].day

        def move(step: int) -> None:
            a = st.session_state["cal_anchor"]
            st.session_state["cal_anchor"] = (a + timedelta(days=7 * step) if st.session_state.get("cal_view") == "week"
                                              else shift_month(a, step))

        def go_to(day: date) -> None:
            st.session_state["cal_anchor"] = day

        view_names = {"month": ":material/calendar_month: Miesiąc", "week": ":material/view_week: Tydzień",
                      "list": ":material/list: Lista"}
        with st.container(horizontal=True, vertical_alignment="center"):
            view = st.session_state.get("cal_view", "month")
            anchor: date = st.session_state["cal_anchor"]
            if view != "list":
                st.button("Dziś", on_click=go_to, args=(today,))
                st.button("", icon=":material/chevron_left:", key="cal_prev", on_click=move, args=(-1,),
                          help="Poprzedni okres", type="tertiary")
                st.button("", icon=":material/chevron_right:", key="cal_next", on_click=move, args=(1,),
                          help="Następny okres", type="tertiary")
                st.markdown(f"#### {period_title(anchor, view)}")
            st.space("stretch")
            view = st.segmented_control("Widok", list(view_names), format_func=view_names.get, default="month",
                                        required=True, key="cal_view", label_visibility="collapsed")

        if view == "list":
            st.caption("Jeden wiersz na dzień i tor - rozwiń, aby zobaczyć każdy wybór (wydarzenie, godzina, sesja, "
                       "grupa) z liczbą wolnych miejsc.")
            for d in days:
                with st.expander(d.label(), key=f"day_{day_key(d)}"):
                    choices_table(d, compact=False)

            st.subheader("Szczegóły wydarzenia")
            by_id = {e.id: e for e in events}
            # Opcje to identyfikatory terminów; gdy filtr usunie wybrany termin, wybór wraca do pierwszego.
            if st.session_state.get("list_event") not in by_id:
                st.session_state.pop("list_event", None)

            def event_option(eid: str) -> str:
                e = by_id[eid]
                when = f"{e.date_start:%d.%m.%Y}" + (f" - {e.date_end:%d.%m.%Y}" if e.date_end else "")
                return " · ".join(x for x in (when, e.start_time, e.track, e.name, e.organizer) if x)

            chosen = st.selectbox("Wybierz termin", list(by_id), key="list_event", format_func=event_option)
            with st.container(border=True):
                event_details(by_id[chosen])
        else:
            weeks = month_weeks(anchor) if view == "month" else [week_of(anchor)]
            header = st.columns(7, gap="xsmall")
            for i, name in enumerate(WEEKDAYS):
                header[i].markdown(f"**{name}**" if i < 5 else f":gray[**{name}**]", text_alignment="center")
            for week in weeks:
                cells = st.columns(7, gap="xsmall")
                for cell, day in zip(cells, week):
                    with cell.container(border=True, height="stretch" if view == "month" else 360, gap="xxsmall"):
                        in_month = view == "week" or day.month == anchor.month
                        num = f"{day.day}" if day.day != 1 else f"{day.day} {MONTHS[day.month - 1][:3]}"
                        if day == today:
                            num = f":primary-background[**{num}**]"
                        elif not in_month:
                            num = f":gray[{num}]"
                        st.markdown(num)
                        for d in on_date.get(day, []):
                            card_key = f"cal_{day_key(d)}_{day:%Y%m%d}"
                            card = st.popover(chip_label(d, colors[d.track], day), type="tertiary",
                                              width="stretch", key=card_key, on_change="rerun")
                            if card.open:  # treść karty liczona tylko po rozwinięciu
                                with card:
                                    day_card(d, card_key)
            wanted = st.session_state.pop("cal_dialog", None)
            if wanted:
                match = next((d for d in days if day_key(d) == wanted), None)
                if match:
                    day_dialog(match)

            # podpowiedź, gdy w widocznym okresie nie ma nic
            first, last = weeks[0][0], weeks[-1][-1]
            if not any(overlaps(d, first, last) for d in days):
                nxt = next((d.day for d in days if d.day > last), None)
                prev = next((d.day for d in reversed(days) if d.day < first), None)
                with st.container(horizontal=True, vertical_alignment="center"):
                    st.caption("Brak terminów w tym okresie.")
                    if prev:
                        st.button(f"Poprzedni: {prev:%d.%m.%Y}", icon=":material/arrow_back:", on_click=go_to,
                                  args=(prev,), key="cal_go_prev")
                    if nxt:
                        st.button(f"Najbliższy: {nxt:%d.%m.%Y}", icon=":material/arrow_forward:", on_click=go_to,
                                  args=(nxt,), key="cal_go_next")
            visible = {d.track for d in days if overlaps(d, first, last)}
            if visible:
                st.caption("Kliknij termin w kalendarzu, aby go rozwinąć. Kolor oznacza tor: "
                           + " · ".join(f":{c}[●] {t}" for t, c in colors.items() if t in visible))

        st.caption(f"Pokazano terminy pasujące do filtrów spośród **{n_saved}** zapisanych w bazie. Każde "
                   "wyszukiwanie dopisuje nowe terminy i aktualizuje już zapisane (bez duplikatów u tego samego "
                   "organizatora).")
        st.download_button("⬇️ Pobierz JSON (kontrakt dla kolejnych agentów)",
                           data=ScoutReport(run_id="export", started_at=pd.Timestamp.now().to_pydatetime(),
                                            model="-", reference_date=date.today(), events=events).to_json_events(),
                           file_name="torowania.json", mime="application/json")

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
