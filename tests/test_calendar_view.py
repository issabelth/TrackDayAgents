"""Pomocnicze funkcje widoku kalendarza (zakładka Terminy)."""

from datetime import date

from trackday.views import month_weeks, period_title, shift_month, week_of


def test_shift_month_crosses_year():
    assert shift_month(date(2026, 11, 17), 1) == date(2026, 12, 1)
    assert shift_month(date(2026, 12, 31), 1) == date(2027, 1, 1)
    assert shift_month(date(2026, 1, 31), -1) == date(2025, 12, 1)


def test_month_weeks_start_on_monday_and_cover_month():
    weeks = month_weeks(date(2026, 11, 17))
    assert all(len(w) == 7 for w in weeks)
    assert weeks[0][0].weekday() == 0
    days = [d for w in weeks for d in w]
    assert date(2026, 11, 1) in days and date(2026, 11, 30) in days


def test_week_of_and_titles():
    week = week_of(date(2026, 11, 18))
    assert week[0] == date(2026, 11, 16) and week[-1] == date(2026, 11, 22)
    assert period_title(date(2026, 11, 18), "month") == "listopad 2026"
    assert period_title(date(2026, 11, 18), "week") == "16-22 listopada 2026"
    assert period_title(date(2026, 12, 30), "week") == "28 gru - 3 sty 2027"


def _event(i, start, end=None, track="Tor Poznań", time=None):
    from trackday.models import TrackDayEvent

    return TrackDayEvent(id=i, name=i, date_start=start, date_end=end, track=track, organizer="X",
                         url="https://x.example/", source_id="s", start_time=time)


def test_multi_day_event_spans_every_day_in_calendar():
    from trackday.views import by_date, chip_label, group_by_day, overlaps

    days = group_by_day([_event("brno", date(2027, 4, 30), date(2027, 5, 2), "Brno", "09:00"),
                         _event("poz", date(2027, 5, 1), time="10:00")])
    brno = next(d for d in days if d.track == "Brno")
    assert brno.n_days == 3 and brno.dates[-1] == date(2027, 5, 2)
    assert brno.date_label() == "pt 30.04 - nd 02.05.2027 (3 dni)"
    cal = by_date(days)
    assert [d.track for d in cal[date(2027, 5, 1)]] == ["Brno", "Tor Poznań"]  # kilkudniowe najpierw
    assert date(2027, 5, 3) not in cal
    assert chip_label(brno, "blue") == ":blue[●] 09:00 Brno · 3 dni"
    assert chip_label(brno, "blue", date(2027, 5, 2)) == ":blue[▬] Brno · dzień 3/3"
    assert overlaps(brno, date(2027, 5, 2), date(2027, 5, 8)) and not overlaps(brno, date(2027, 5, 3), date(2027, 5, 9))


def test_same_start_but_different_length_are_separate_entries():
    from trackday.views import day_key, group_by_day

    days = group_by_day([_event("a", date(2027, 5, 1)), _event("b", date(2027, 5, 1), date(2027, 5, 2))])
    assert len(days) == 2 and len({day_key(d) for d in days}) == 2
