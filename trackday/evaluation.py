"""Porównanie wyników agenta z zestawem wzorcowym (precyzja/pełność, zgodność pól)."""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path

from trackday.config import PROJECT_ROOT
from trackday.models import TrackDayEvent
from trackday.tools.normalize import event_id, match_track, normalize_organizer

GOLDEN_PATH = PROJECT_ROOT / "tests" / "golden" / "expected_events.json"
FIELDS = ["date_end", "price", "currency", "seats_left", "url"]


def load_golden(path: Path = GOLDEN_PATH) -> tuple[date, list[dict]]:
    raw = json.loads(path.read_text(encoding="utf-8"))
    return date.fromisoformat(raw["reference_date"]), raw["events"]


def _key(organizer: str, track: str, d: str) -> tuple[str, str, str]:
    t = match_track(track)
    return normalize_organizer(organizer), t.name if t else track, d


@dataclass
class EvalResult:
    expected: int
    extracted: int
    matched: int
    field_correct: dict[str, int] = field(default_factory=dict)
    missing: list[str] = field(default_factory=list)
    unexpected: list[str] = field(default_factory=list)
    mismatches: list[str] = field(default_factory=list)

    @property
    def precision(self) -> float:
        return self.matched / self.extracted if self.extracted else 0.0

    @property
    def recall(self) -> float:
        return self.matched / self.expected if self.expected else 0.0

    def field_accuracy(self, f: str) -> float:
        return self.field_correct.get(f, 0) / self.matched if self.matched else 0.0


def _eq(f: str, exp, got) -> bool:
    if f == "price":
        return (exp is None and got is None) or (exp is not None and got is not None and abs(exp - got) < 0.01)
    return exp == got


def evaluate(events: list[TrackDayEvent], golden: list[dict]) -> EvalResult:
    got = {_key(e.organizer, e.track, e.date_start.isoformat()): e for e in events}
    res = EvalResult(expected=len(golden), extracted=len(events), matched=0,
                     field_correct={f: 0 for f in FIELDS})
    seen = set()
    for g in golden:
        k = _key(g["organizer"], g["track"], g["date_start"])
        e = got.get(k)
        label = f"{g['date_start']} {g['track']} ({g['organizer']})"
        if e is None:
            res.missing.append(label)
            continue
        seen.add(k)
        res.matched += 1
        values = {"date_end": e.date_end.isoformat() if e.date_end else None, "price": e.price,
                  "currency": e.currency, "seats_left": e.seats_left, "url": e.url}
        for f in FIELDS:
            if _eq(f, g[f], values[f]):
                res.field_correct[f] += 1
            else:
                res.mismatches.append(f"{label}: {f} oczekiwano={g[f]!r}, otrzymano={values[f]!r}")
    for k, e in got.items():
        if k not in seen:
            res.unexpected.append(f"{e.date_start} {e.track} ({e.organizer})")
    return res


def golden_as_events() -> list[TrackDayEvent]:
    """Zestaw wzorcowy jako wydarzenia - do trybu demo w UI (bez wywołania LLM)."""
    _, golden = load_golden()
    out = []
    from trackday.agents.event_scout import FX_TO_PLN

    for g in golden:
        t = match_track(g["track"])
        out.append(TrackDayEvent(
            id=event_id(g["organizer"], g["track"], date.fromisoformat(g["date_start"])),
            name=f"Torowanie {g['track']}",
            date_start=date.fromisoformat(g["date_start"]),
            date_end=date.fromisoformat(g["date_end"]) if g["date_end"] else None,
            track=t.name if t else g["track"], track_known=t is not None,
            organizer=g["organizer"], price=g["price"], currency=g["currency"],
            price_pln=round(g["price"] * FX_TO_PLN[g["currency"]], 2) if g["price"] is not None else None,
            url=g["url"], seats_left=g["seats_left"], confidence=1.0,
            lat=t.lat if t else None, lon=t.lon if t else None, source_id="demo",
            issues=["dane demonstracyjne (zestaw wzorcowy)"],
        ))
    return out
