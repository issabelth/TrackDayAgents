"""Repozytorium SQLite: wydarzenia, historia liczby miejsc (pod Agenta 4) i przebiegi agenta."""

from __future__ import annotations

import json
import sqlite3
from contextlib import contextmanager
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
    last_seen     TEXT NOT NULL
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


class EventRepository:
    def __init__(self, path: Path | str):
        self.path = str(path)
        if self.path != ":memory:":
            Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        self._mem_conn = sqlite3.connect(":memory:") if self.path == ":memory:" else None
        with self._conn() as c:
            c.executescript(SCHEMA)

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
    def upsert_events(self, events: list[TrackDayEvent]) -> None:
        now = datetime.now().isoformat(timespec="seconds")
        with self._conn() as c:
            for e in events:
                c.execute(
                    """INSERT INTO events (id, date_start, date_end, track, organizer, price_pln, seats_left,
                                           data, first_seen, last_seen)
                       VALUES (?,?,?,?,?,?,?,?,?,?)
                       ON CONFLICT(id) DO UPDATE SET
                         date_end=excluded.date_end, track=excluded.track, organizer=excluded.organizer,
                         price_pln=excluded.price_pln, seats_left=excluded.seats_left,
                         data=excluded.data, last_seen=excluded.last_seen""",
                    (e.id, e.date_start.isoformat(), e.date_end.isoformat() if e.date_end else None,
                     e.track, e.organizer, e.price_pln, e.seats_left, e.model_dump_json(), now, now),
                )
                c.execute("INSERT OR REPLACE INTO seat_history VALUES (?,?,?)", (e.id, now, e.seats_left))

    def save_run(self, report: ScoutReport) -> None:
        self.upsert_events(report.events)
        with self._conn() as c:
            # Udany przebieg źródła zastępuje jego poprzednie terminy: wpisy, których nie ma już na stronie
            # (odwołane, wyprzedane i zdjęte, albo odczytane wcześniej inną metodą), są usuwane.
            new_ids = [e.id for e in report.events]
            for sr in report.sources:
                if not sr.ok or sr.source_id == "pasted":
                    continue
                c.execute(f"""DELETE FROM events WHERE json_extract(data, '$.source_id') = ?
                              AND id NOT IN ({",".join("?" * len(new_ids))})""", (sr.source_id, *new_ids))
            c.execute("INSERT OR REPLACE INTO runs VALUES (?,?,?,?,?)",
                      (report.run_id, report.started_at.isoformat(timespec="seconds"), report.model,
                       len(report.events), report.model_dump_json()))

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
                    organizers: list[str] | None = None, only_available: bool = False) -> list[TrackDayEvent]:
        sql = "SELECT data FROM events WHERE 1=1"
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
        if only_available:
            sql += " AND (seats_left IS NULL OR seats_left > 0)"
        sql += " ORDER BY date_start, track"
        with self._conn() as c:
            return [TrackDayEvent.model_validate_json(r[0]) for r in c.execute(sql, args)]

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
