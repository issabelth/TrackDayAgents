"""Normalizacja nazw torów i deduplikacja wydarzeń."""

from __future__ import annotations

import hashlib
import json
import re
import unicodedata
from dataclasses import dataclass
from datetime import date
from functools import lru_cache

from trackday.config import DATA_DIR
from trackday.models import TrackDayEvent


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


@lru_cache(maxsize=1)
def load_tracks() -> tuple[Track, ...]:
    raw = json.loads((DATA_DIR / "tracks.json").read_text(encoding="utf-8"))
    return tuple(
        Track(t["name"], t["country"], t["lat"], t["lon"], tuple(_fold(a) for a in t["aliases"] + [t["name"]]))
        for t in raw["tracks"]
    )


def match_track(name: str) -> Track | None:
    """Dopasowuje nazwę toru z treści do znanej listy torów (dokładnie lub po fragmencie)."""
    folded = _fold(name)
    if not folded:
        return None
    tracks = load_tracks()
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
                    if not (c.source_id == e.source_id and c.url != e.url)), None)
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
        merged = primary.model_copy(update=updates)
        merged.issues = sorted(set(primary.issues) | {"połączono z duplikatem z innego źródła"})
        group[idx] = merged
    events_out = [e for group in by_key.values() for e in group]
    return sorted(events_out, key=lambda e: (e.date_start, e.track, e.name)), removed
