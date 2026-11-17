"""Repozytorium SQLite: wydarzenia, historia liczby miejsc (pod Agenta 4) i przebiegi agenta."""

from __future__ import annotations

import hashlib
import sqlite3
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import date, datetime
from pathlib import Path
from typing import Iterator

from trackday.models import ScoutReport, Source, TrackDayEvent, TrackDef

SCHEMA = """
CREATE TABLE IF NOT EXISTS events (
    id            TEXT PRIMARY KEY,
    date_start    TEXT NOT NULL,
    date_end      TEXT,
    track         TEXT NOT NULL,
    organizer     TEXT NOT NULL,
    price_pln     REAL,
    seats_left    INTEGER,
    data          TEXT NOT NULL,          -- pełny JSON TrackDayEvent
    first_seen    TEXT NOT NULL,
    last_seen     TEXT NOT NULL,
    missing_since TEXT                    -- od kiedy terminu nie ma na stronie źródła (NULL = jest)
);
CREATE INDEX IF NOT EXISTS idx_events_date ON events(date_start);

CREATE TABLE IF NOT EXISTS seat_history (
    event_id    TEXT NOT NULL REFERENCES events(id),
    checked_at  TEXT NOT NULL,
    seats_left  INTEGER,
    PRIMARY KEY (event_id, checked_at)
);

CREATE TABLE IF NOT EXISTS runs (
    run_id      TEXT PRIMARY KEY,
    started_at  TEXT NOT NULL,
    model       TEXT NOT NULL,
    n_events    INTEGER NOT NULL,
    report      TEXT NOT NULL             -- pełny JSON ScoutReport
);

-- Stała lista źródeł przeszukiwanych przez agenta (edytowana w zakładce "Źródła")
CREATE TABLE IF NOT EXISTS sources (
    id          TEXT PRIMARY KEY,
    position    INTEGER NOT NULL,
    data        TEXT NOT NULL             -- pełny JSON Source
);

-- Lista znanych torów z współrzędnymi (edytowana w zakładce "Tory")
CREATE TABLE IF NOT EXISTS tracks (
    position    INTEGER PRIMARY KEY,
    data        TEXT NOT NULL             -- pełny JSON TrackDef
);

CREATE TABLE IF NOT EXISTS meta (
    key         TEXT PRIMARY KEY,
    value       TEXT
);
"""


@dataclass
class UpsertResult:
    added: int = 0
    updated: int = 0
    ids: list[str] = field(default_factory=list)  # id w bazie, w kolejności zapisywanych terminów


class EventRepository:
    def __init__(self, path: Path | str):
        self.path = str(path)
        if self.path != ":memory:":
            Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        self._mem_conn = sqlite3.connect(":memory:") if self.path == ":memory:" else None
        with self._conn() as c:
            c.executescript(SCHEMA)
            cols = {r[1] for r in c.execute("PRAGMA table_info(events)")}
            if "missing_since" not in cols:  # baza z wcześniejszej wersji aplikacji
                c.execute("ALTER TABLE events ADD COLUMN missing_since TEXT")

    @contextmanager
    def _conn(self) -> Iterator[sqlite3.Connection]:
        conn = self._mem_conn or sqlite3.connect(self.path)
        try:
            yield conn
            conn.commit()
        finally:
            if self._mem_conn is None:
                conn.close()

    # ---------------------------------------------------------------- zapis
    def upsert_events(self, events: list[TrackDayEvent]) -> UpsertResult:
        """Zapisuje terminy w bazie na stałe. Termin, który już jest zapisany u tego samego organizatora
        (ten sam dzień, tor i sesja - zob. ``same_stored_event``), jest aktualizowany pod dotychczasowym id,
        więc kolejne wyszukiwania nie tworzą duplikatów, a historia liczby miejsc jest ciągła."""
        from trackday.tools.normalize import merge_stored, same_stored_event

        now = datetime.now().replace(microsecond=0)
        res = UpsertResult()
        with self._conn() as c:
            # kandydaci do aktualizacji: zapisane terminy z tych samych dni
            dates = sorted({e.date_start.isoformat() for e in events})
            stored: dict[str, TrackDayEvent] = {}
            for i in range(0, len(dates), 500):
                part = dates[i:i + 500]
                for data, first in c.execute(f"SELECT data, first_seen FROM events WHERE date_start IN "
                                             f"({','.join('?' * len(part))})", part):
                    ev = TrackDayEvent.model_validate_json(data)
                    stored[ev.id] = ev.model_copy(update={"first_seen": datetime.fromisoformat(first)})
            claimed: set[str] = set()  # zapisany termin aktualizuje co najwyżej jeden nowy z tej partii

            for e in events:
                cands = [o for o in stored.values() if o.id not in claimed and same_stored_event(o, e)]
                # najpierw ten sam id, potem ta sama godzina, potem ten sam link
                cands.sort(key=lambda o: (o.id != e.id, o.start_time != e.start_time, o.url != e.url))
                if cands:
                    new = merge_stored(cands[0], e)
                    res.updated += 1
                else:
                    new_id, n = e.id, 0
                    while new_id in stored or new_id in claimed or c.execute(
                            "SELECT 1 FROM events WHERE id=?", (new_id,)).fetchone():
                        n += 1  # id zajęte przez inny termin (np. druga sesja tego dnia bez osobnej podstrony)
                        new_id = hashlib.sha1(f"{e.id}|{e.start_time}|{e.track_layout}|{e.url}|{n}".encode()
                                              ).hexdigest()[:12]
                    new = e.model_copy(update={"id": new_id, "first_seen": now})
                    res.added += 1
                new = new.model_copy(update={"first_seen": new.first_seen or now, "last_seen": now,
                                             "missing_since": None})
                claimed.add(new.id)
                stored[new.id] = new
                res.ids.append(new.id)
                c.execute(
                    """INSERT INTO events (id, date_start, date_end, track, organizer, price_pln, seats_left,
                                           data, first_seen, last_seen, missing_since)
                       VALUES (?,?,?,?,?,?,?,?,?,?,NULL)
                       ON CONFLICT(id) DO UPDATE SET
                         date_start=excluded.date_start, date_end=excluded.date_end, track=excluded.track,
                         organizer=excluded.organizer, price_pln=excluded.price_pln,
                         seats_left=excluded.seats_left, data=excluded.data, last_seen=excluded.last_seen,
                         missing_since=NULL""",
                    (new.id, new.date_start.isoformat(), new.date_end.isoformat() if new.date_end else None,
                     new.track, new.organizer, new.price_pln, new.seats_left, new.model_dump_json(),
                     new.first_seen.isoformat(timespec="seconds"), now.isoformat()),
                )
                c.execute("INSERT OR REPLACE INTO seat_history VALUES (?,?,?)",
                          (new.id, now.isoformat(), new.seats_left))
        return res

    def save_run(self, report: ScoutReport) -> UpsertResult:
        """Zapisuje wynik przebiegu agenta. Terminy z wcześniejszych wyszukiwań zostają w bazie. Jeśli udany
        przebieg źródła nie znalazł już jakiegoś zapisanego terminu (odwołany, zdjęty ze strony), termin
        dostaje znacznik ``missing_since`` - widać go w widoku i można go ukryć filtrem."""
        res = self.upsert_events(report.events)
        report.events = [e.model_copy(update={"id": i}) for e, i in zip(report.events, res.ids)]
        now = datetime.now().isoformat(timespec="seconds")
        with self._conn() as c:
            for sr in report.sources:
                if not sr.ok or sr.source_id == "pasted":
                    continue
                c.execute(f"""UPDATE events SET missing_since = ?
                              WHERE json_extract(data, '$.source_id') = ? AND missing_since IS NULL
                              AND id NOT IN ({",".join("?" * len(res.ids))})""", (now, sr.source_id, *res.ids))
            c.execute("INSERT OR REPLACE INTO runs VALUES (?,?,?,?,?)",
                      (report.run_id, report.started_at.isoformat(timespec="seconds"), report.model,
                       len(report.events), report.model_dump_json()))
        return res

    def clear(self) -> None:
        """Usuwa zebrane terminy i przebiegi agenta. Lista źródeł zostaje."""
        with self._conn() as c:
            c.executescript("DELETE FROM seat_history; DELETE FROM events; DELETE FROM runs;")

    # ---------------------------------------------------------------- źródła
    def list_sources(self) -> list[Source]:
        with self._conn() as c:
            return [Source.model_validate_json(r[0])
                    for r in c.execute("SELECT data FROM sources ORDER BY position")]

    def save_sources(self, sources: list[Source]) -> None:
        """Zastępuje całą listę źródeł (kolejność = kolejność na liście)."""
        with self._conn() as c:
            c.execute("DELETE FROM sources")
            c.executemany("INSERT INTO sources VALUES (?,?,?)",
                          [(s.id, i, s.model_dump_json()) for i, s in enumerate(sources)])
            c.execute("INSERT OR REPLACE INTO meta VALUES ('sources_initialized', '1')")

    # ---------------------------------------------------------------- tory
    def list_tracks(self) -> list[TrackDef]:
        with self._conn() as c:
            return [TrackDef.model_validate_json(r[0]) for r in c.execute("SELECT data FROM tracks ORDER BY position")]

    def save_tracks(self, tracks: list[TrackDef]) -> None:
        with self._conn() as c:
            c.execute("DELETE FROM tracks")
            c.executemany("INSERT INTO tracks VALUES (?,?)", [(i, t.model_dump_json()) for i, t in enumerate(tracks)])
            c.execute("INSERT OR REPLACE INTO meta VALUES ('tracks_initialized', '1')")

    def init_tracks(self, defaults: list[TrackDef]) -> None:
        """Przy pierwszym uruchomieniu wpisuje tory domyślne (data/tracks.json)."""
        with self._conn() as c:
            done = c.execute("SELECT 1 FROM meta WHERE key='tracks_initialized'").fetchone()
        if not done:
            self.save_tracks(defaults)

    def relink_tracks(self, tracks) -> int:
        """Ponownie dopasowuje tory zapisanych terminów do aktualnej listy torów (nazwa, współrzędne).
        Zwraca liczbę zmienionych terminów."""
        from trackday.tools.normalize import match_track

        unknown = "tor spoza listy znanych torów"
        changed = []
        for e in self.list_events():
            t = match_track(e.track, tracks)
            if t:
                upd = {"track": t.name, "track_known": True, "lat": t.lat, "lon": t.lon,
                       "issues": [i for i in e.issues if i != unknown]}
            else:
                upd = {"track_known": False, "lat": None, "lon": None,
                       "issues": e.issues if unknown in e.issues else e.issues + [unknown]}
            new = e.model_copy(update=upd)
            if new != e:
                changed.append(new)
        with self._conn() as c:
            for e in changed:
                c.execute("UPDATE events SET track=?, data=? WHERE id=?", (e.track, e.model_dump_json(), e.id))
        return len(changed)

    def init_sources(self, defaults: list[Source]) -> None:
        """Przy pierwszym uruchomieniu wpisuje źródła domyślne. Później nie nadpisuje listy,
        nawet gdy użytkownik usunie wszystkie wpisy."""
        with self._conn() as c:
            done = c.execute("SELECT 1 FROM meta WHERE key='sources_initialized'").fetchone()
        if not done:
            self.save_sources(defaults)

    # ---------------------------------------------------------------- odczyt
    def list_events(self, date_from: date | None = None, date_to: date | None = None,
                    max_price_pln: float | None = None, tracks: list[str] | None = None,
                    organizers: list[str] | None = None, only_available: bool = False,
                    min_price_pln: float | None = None, weekdays: list[int] | None = None,
                    hide_missing: bool = False) -> list[TrackDayEvent]:
        """``weekdays`` - dni tygodnia jak w ``date.weekday()`` (0 = poniedziałek … 6 = niedziela).
        Filtry ceny nie odrzucają terminów bez ceny (lepiej je pokazać niż przeoczyć)."""
        sql = "SELECT data, first_seen, last_seen, missing_since FROM events WHERE 1=1"
        args: list = []
        if date_from:
            sql += " AND date_start >= ?"
            args.append(date_from.isoformat())
        if date_to:
            sql += " AND date_start <= ?"
            args.append(date_to.isoformat())
        if max_price_pln is not None:
            sql += " AND (price_pln IS NULL OR price_pln <= ?)"
            args.append(max_price_pln)
        if tracks:
            sql += f" AND track IN ({','.join('?' * len(tracks))})"
            args.extend(tracks)
        if organizers:
            sql += f" AND organizer IN ({','.join('?' * len(organizers))})"
            args.extend(organizers)
        if min_price_pln is not None:
            sql += " AND (price_pln IS NULL OR price_pln >= ?)"
            args.append(min_price_pln)
        if weekdays:
            # SQLite: %w = 0 niedziela … 6 sobota
            sql += f" AND CAST(strftime('%w', date_start) AS INTEGER) IN ({','.join('?' * len(weekdays))})"
            args.extend((d + 1) % 7 for d in weekdays)
        if only_available:
            sql += " AND (seats_left IS NULL OR seats_left > 0)"
        if hide_missing:
            sql += " AND missing_since IS NULL"
        sql += " ORDER BY date_start, track"
        out = []
        with self._conn() as c:
            for data, first, last, missing in c.execute(sql, args):
                ev = TrackDayEvent.model_validate_json(data)
                out.append(ev.model_copy(update={
                    "first_seen": datetime.fromisoformat(first), "last_seen": datetime.fromisoformat(last),
                    "missing_since": datetime.fromisoformat(missing) if missing else None}))
        return out

    def distinct(self, column: str) -> list[str]:
        if column not in ("track", "organizer"):
            raise ValueError("niedozwolona kolumna")
        with self._conn() as c:
            return [r[0] for r in c.execute(f"SELECT DISTINCT {column} FROM events ORDER BY 1")]

    def last_run(self) -> ScoutReport | None:
        with self._conn() as c:
            row = c.execute("SELECT report FROM runs ORDER BY started_at DESC LIMIT 1").fetchone()
        return ScoutReport.model_validate_json(row[0]) if row else None

    def seat_history(self, event_id: str) -> list[tuple[str, int | None]]:
        with self._conn() as c:
            return list(c.execute("SELECT checked_at, seats_left FROM seat_history WHERE event_id=? "
                                  "ORDER BY checked_at", (event_id,)))
