"""Normalizacja nazw torów i deduplikacja wydarzeń."""

from __future__ import annotations

import hashlib
import json
import re
import unicodedata
from urllib.parse import quote_plus
from dataclasses import dataclass
from datetime import date
from functools import lru_cache

from trackday.config import DATA_DIR
from trackday.models import TrackDayEvent, TrackDef


@dataclass(frozen=True)
class Track:
    name: str
    country: str
    lat: float
    lon: float
    aliases: tuple[str, ...]


def _fold(text: str) -> str:
    """Małe litery, bez polskich znaków i interpunkcji - do porównań."""
    text = unicodedata.normalize("NFKD", text.lower()).replace("ł", "l")
    text = "".join(c for c in text if not unicodedata.combining(c))
    return re.sub(r"[^a-z0-9]+", " ", text).strip()


def default_track_defs() -> list[TrackDef]:
    """Tory domyślne z pliku - przy pierwszym uruchomieniu trafiają do bazy, potem edytuje je użytkownik."""
    raw = json.loads((DATA_DIR / "tracks.json").read_text(encoding="utf-8"))
    return [TrackDef.model_validate(t) for t in raw["tracks"]]


def build_tracks(defs: list[TrackDef]) -> tuple[Track, ...]:
    return tuple(Track(d.name, d.country or "", d.lat, d.lon,
                       tuple(a for a in (_fold(x) for x in d.aliases + [d.name]) if a))
                 for d in defs)


@lru_cache(maxsize=1)
def load_tracks() -> tuple[Track, ...]:
    return build_tracks(default_track_defs())


def match_track(name: str, tracks: tuple[Track, ...] | None = None) -> Track | None:
    """Dopasowuje nazwę toru z treści do listy znanych torów (dokładnie lub po fragmencie).

    ``tracks`` - lista torów (np. z bazy, edytowana przez użytkownika); domyślnie tory z data/tracks.json.
    """
    folded = _fold(name)
    if not folded:
        return None
    tracks = load_tracks() if tracks is None else tracks
    for t in tracks:
        if folded in t.aliases:
            return t
    # dłuższe aliasy najpierw, żeby 'tor poznan' wygrał z 'poznan'
    candidates = sorted(((a, t) for t in tracks for a in t.aliases), key=lambda x: -len(x[0]))
    for alias, t in candidates:
        if len(alias) >= 4 and re.search(rf"\b{re.escape(alias)}\b", folded):
            return t
    return None


def normalize_organizer(name: str) -> str:
    return _fold(name)


def event_key(organizer: str, track: str, date_start: date) -> str:
    return f"{normalize_organizer(organizer)}|{_fold(track)}|{date_start.isoformat()}"


def event_id(organizer: str, track: str, date_start: date, extra: str = "") -> str:
    """``extra`` (np. adres podstrony) odróżnia kilka sesji tego samego dnia na tym samym torze."""
    key = event_key(organizer, track, date_start) + (f"|{extra}" if extra else "")
    return hashlib.sha1(key.encode()).hexdigest()[:12]


_MERGE_FIELDS = ["date_end", "price", "currency", "price_pln", "price_note", "seats_left",
                 "seats_total", "availability_note", "lat", "lon"]


def _different_session(a: TrackDayEvent, b: TrackDayEvent) -> bool:
    """Ten sam dzień i tor, ale inna godzina startu albo inna odmiana toru (np. REVERS) - to osobne terminy."""
    times = a.start_time and b.start_time and a.start_time != b.start_time
    layouts = (a.track_layout or "").lower() != (b.track_layout or "").lower() and (a.track_layout or b.track_layout)
    return bool(times or (layouts and a.source_id == b.source_id))


def deduplicate(events: list[TrackDayEvent], own_source: dict[str, str]) -> tuple[list[TrackDayEvent], int]:
    """Łączy duplikaty (ten sam organizator + tor + data).

    Pierwszeństwo ma wpis ze strony samego organizatora (``own_source``: source_id -> organizator),
    puste pola są uzupełniane danymi z duplikatu. Dwa wpisy z TEGO SAMEGO źródła z różnymi linkami
    (np. dwie sesje tego samego dnia, każda na osobnej podstronie) nie są duplikatami.
    Zwraca (lista, liczba usuniętych).
    """
    by_key: dict[str, list[TrackDayEvent]] = {}
    removed = 0

    def is_own(e: TrackDayEvent) -> bool:
        org = own_source.get(e.source_id)
        return org is not None and normalize_organizer(org) == normalize_organizer(e.organizer)

    for e in events:
        k = event_key(e.organizer, e.track, e.date_start)
        group = by_key.setdefault(k, [])
        idx = next((i for i, c in enumerate(group)
                    if not (c.source_id == e.source_id and c.url != e.url) and not _different_session(c, e)), None)
        if idx is None:
            group.append(e)
            continue
        removed += 1
        current = group[idx]
        primary, secondary = (e, current) if (is_own(e) and not is_own(current)) else (current, e)
        updates = {f: getattr(secondary, f) for f in _MERGE_FIELDS
                   if getattr(primary, f) is None and getattr(secondary, f) is not None}
        if not primary.groups and secondary.groups:
            updates["groups"] = secondary.groups
        if not primary.variants and secondary.variants:
            updates["variants"] = secondary.variants
        merged = primary.model_copy(update=updates)
        merged.issues = sorted(set(primary.issues) | {"połączono z duplikatem z innego źródła"})
        group[idx] = merged
    events_out = [e for group in by_key.values() for e in group]
    return sorted(events_out, key=lambda e: (e.date_start, e.track, e.name)), removed


_NUM = r"[-+]?\d+(?:[.,]\d+)?"


def parse_coords(text: str) -> tuple[float, float]:
    """Współrzędne wpisane przez użytkownika -> (szerokość, długość).

    Przyjmuje format kopiowany z Map Google („52.4176, 16.8089”), a także „52,4176; 16,8089”
    i „52.4176 16.8089”. Zgłasza ValueError przy błędnym formacie lub wartościach spoza zakresu.
    """
    text = (text or "").strip()
    m = re.fullmatch(rf"\(?\s*({_NUM})\s*[,; ]\s*({_NUM})\s*\)?", text)
    if not m:
        raise ValueError(f"„{text}” - wpisz dwie liczby: szerokość i długość, np. 52.4176, 16.8089")
    lat, lon = (float(x.replace(",", ".")) for x in m.groups())
    if not -90 <= lat <= 90 or not -180 <= lon <= 180:
        raise ValueError(f"„{text}” - szerokość musi być w zakresie -90..90, długość -180..180")
    return lat, lon


COL_NAME, COL_COUNTRY, COL_LINK, COL_COORDS, COL_ALIASES = (
    "Nazwa", "Kraj", "Link do Map Google", "Współrzędne (szer., dł.)", "Inne nazwy (po przecinku)")
TRACK_COLUMNS = [COL_NAME, COL_COUNTRY, COL_LINK, COL_COORDS, COL_ALIASES]
GMAPS_COLUMN = "Mapy Google"


def track_row(t: TrackDef) -> dict:
    """Tor -> wiersz tabeli w zakładce „Tory” (z linkiem „otwórz” do Map Google)."""
    return {COL_NAME: t.name, COL_COUNTRY: t.country or "", COL_LINK: t.maps_url or "",
            COL_COORDS: f"{t.lat:.5f}, {t.lon:.5f}", COL_ALIASES: ", ".join(t.aliases),
            GMAPS_COLUMN: t.maps_url or google_maps_url(t.lat, t.lon)}


def google_maps_url(lat: float | None = None, lon: float | None = None, query: str | None = None) -> str:
    """Link do Map Google: punkt o podanych współrzędnych albo wyszukiwanie po nazwie."""
    q = f"{lat:.5f},{lon:.5f}" if lat is not None and lon is not None else (query or "").strip()
    return "https://www.google.com/maps/search/?api=1&query=" + quote_plus(q)


def _cell(v) -> str:
    if v is None or (isinstance(v, float) and v != v):  # None / NaN z nowych wierszy tabeli
        return ""
    return str(v).strip()


def tracks_from_rows(rows: list[dict], known_links: dict[str, tuple[float, float]] | None = None,
                     resolve_link=None) -> tuple[list[TrackDef], list[str]]:
    """Wiersze tabeli z zakładki „Tory” -> (lista torów, błędy). Puste wiersze są pomijane.

    Tor podaje się współrzędnymi albo linkiem do obiektu w Mapach Google. Nowy (zmieniony) link ma
    pierwszeństwo - współrzędne są z niego odczytywane; dla linku już zapisanego (``known_links``)
    obowiązują współrzędne z tabeli, więc można je poprawić ręcznie bez ponownego otwierania linku.
    """
    from trackday.tools.maps_link import resolve_maps_link

    resolve_link = resolve_link or resolve_maps_link
    known_links = known_links or {}
    tracks: list[TrackDef] = []
    errors: list[str] = []
    names: set[str] = set()
    for i, row in enumerate(rows, start=1):
        name, coords, link = (_cell(row.get(c)) for c in (COL_NAME, COL_COORDS, COL_LINK))
        if not name and not coords and not link:
            continue
        if not name:
            errors.append(f"Wiersz {i}: brak nazwy toru.")
            continue
        if _fold(name) in names:
            errors.append(f"Wiersz {i}: tor „{name}” jest już na liście.")
            continue
        names.add(_fold(name))
        try:
            if link and (link not in known_links or not coords):
                lat, lon = resolve_link(link)
            elif coords:
                lat, lon = parse_coords(coords)
            else:
                raise ValueError("wpisz współrzędne albo wklej link do toru z Map Google")
            aliases = [a.strip() for a in _cell(row.get(COL_ALIASES)).split(",") if a.strip()]
            tracks.append(TrackDef(name=name, country=_cell(row.get(COL_COUNTRY)) or None,
                                   lat=lat, lon=lon, aliases=aliases, maps_url=link or None))
        except ValueError as err:  # także błędy walidacji Pydantic
            errors.append(f"Wiersz {i} ({name}): {str(err).splitlines()[0]}")
    return tracks, errors
