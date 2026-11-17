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
