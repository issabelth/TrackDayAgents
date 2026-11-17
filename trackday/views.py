"""Przygotowanie danych do widoku: terminy pogrupowane po dniach, a w każdym dniu - wszystkie wybory
(wydarzenie × sesja × grupa) z liczbą wolnych miejsc. Logika bez Streamlit, żeby dało się ją testować."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date

from trackday.models import TrackDayEvent

WEEKDAYS = ["pon", "wt", "śr", "czw", "pt", "sob", "nd"]


@dataclass
class DayGroup:
    day: date
    track: str
    organizers: list[str]
    events: list[TrackDayEvent]
    rows: list[dict] = field(default_factory=list)

    @property
    def seats_left(self) -> int | None:
        known = [r["Wolne miejsca"] for r in self.rows if isinstance(r["Wolne miejsca"], int)]
        return sum(known) if known else None

    @property
    def min_price(self) -> float | None:
        prices = [r["Cena [zł]"] for r in self.rows if r["Cena [zł]"] is not None]
        return min(prices) if prices else None

    def label(self) -> str:
        n_ev, n_rows = len(self.events), len(self.rows)
        parts = [f"**{WEEKDAYS[self.day.weekday()]} {self.day:%d.%m.%Y}**", self.track, ", ".join(self.organizers),
                 f"{n_ev} {'wydarzenie' if n_ev == 1 else 'wydarzenia' if n_ev < 5 else 'wydarzeń'}"
                 + (f", {n_rows} wyborów" if n_rows != n_ev else "")]
        if self.min_price is not None:
            parts.append(f"od {self.min_price:.0f} zł")
        seats = self.seats_left
        parts.append(f"wolne miejsca: {seats}" if seats is not None else "wolne miejsca: ?")
        return " · ".join(parts)


def all_groups(events: list[TrackDayEvent]) -> list[str]:
    """Wszystkie grupy/poziomy występujące w terminach (do filtra)."""
    found: dict[str, None] = {}
    for e in events:
        for g in [v.group for v in e.variants if v.group] or e.groups:
            found.setdefault(g, None)
    return sorted(found)


def _seats(value: int | None, note: str | None) -> int | str:
    return value if value is not None else (note or "?")


def choice_rows(e: TrackDayEvent, groups: set[str] | None = None, only_available: bool = False) -> list[dict]:
    """Wiersze „wyborów” jednego wydarzenia: po jednym na wariant (sesja × grupa) albo jeden, gdy brak wariantów."""
    base = {
        "Godzina": e.start_time or "",
        "Wydarzenie": e.name,
        "Nitka / odmiana": e.track_layout or "",
        "Organizator": e.organizer,
    }
    rows = []
    notes = "; ".join(([f"nie ma go już na stronie (od {e.missing_since:%d.%m.%Y})"] if e.missing_since else [])
                      + e.issues)
    if e.variants:
        for v in e.variants:
            if groups and v.group not in groups:
                continue
            if only_available and v.seats_left == 0:
                continue
            price = v.price if v.price is not None else e.price
            currency = v.currency or e.currency
            rows.append(base | {
                "Sesja": v.session or "",
                "Grupa": v.group or ", ".join(f"{k}: {val}" for k, val in v.options.items()),
                "Wolne miejsca": _seats(v.seats_left, v.availability_note),
                "Cena": f"{price:g} {currency}" if price is not None else "-",
                "Cena [zł]": price if currency == "PLN" else e.price_pln,
                "Link": e.url, "Uwagi": notes,
            })
    else:
        if groups and not groups & set(e.groups):
            return []
        if only_available and e.seats_left == 0:
            return []
        seats = _seats(e.seats_left, e.availability_note)
        if isinstance(seats, int) and e.seats_total:
            seats = f"{seats} / {e.seats_total}"
        rows.append(base | {
            "Sesja": "",
            "Grupa": ", ".join(e.groups),
            "Wolne miejsca": seats,
            "Cena": f"{e.price:g} {e.currency}" if e.price is not None else "-",
            "Cena [zł]": e.price_pln,
            "Link": e.url, "Uwagi": notes,
        })
    return rows


def group_by_day(events: list[TrackDayEvent], groups: set[str] | None = None,
                 only_available: bool = False) -> list[DayGroup]:
    """Jeden wpis na dzień i tor; wydarzenia bez pasujących wyborów (filtr grup / wolnych miejsc) są pomijane."""
    days: dict[tuple[date, str], DayGroup] = {}
    for e in sorted(events, key=lambda e: (e.date_start, e.track, e.start_time or "", e.name)):
        rows = choice_rows(e, groups, only_available)
        if not rows:
            continue
        d = days.setdefault((e.date_start, e.track), DayGroup(e.date_start, e.track, [], []))
        d.events.append(e)
        d.rows.extend(rows)
        if e.organizer not in d.organizers:
            d.organizers.append(e.organizer)
    return list(days.values())
