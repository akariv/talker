"""Tests for the synthesised calendar filter modes.

Runs with `cd server && pytest tests/`.
"""

import os
import sys
from datetime import datetime
from zoneinfo import ZoneInfo

import icalendar

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import calendars  # noqa: E402

TZ = ZoneInfo("Europe/Berlin")


def _lesson(day: datetime, start: tuple[int, int], end: tuple[int, int],
            summary: str = "lesson") -> icalendar.Event:
    ev = icalendar.Event()
    ev.add("SUMMARY", summary)
    ev.add("DTSTART", day.replace(hour=start[0], minute=start[1], tzinfo=TZ))
    ev.add("DTEND", day.replace(hour=end[0], minute=end[1], tzinfo=TZ))
    return ev


def _timetable() -> icalendar.Calendar:
    """Thu 24 Sep: 09:15–14:00 (three lessons); Fri 25 Sep: 08:30–12:00."""
    cal = icalendar.Calendar()
    thu = datetime(2026, 9, 24)
    fri = datetime(2026, 9, 25)
    for ev in (_lesson(thu, (11, 15), (12, 0)),   # deliberately out of order
               _lesson(thu, (9, 15), (10, 0)),
               _lesson(thu, (13, 0), (14, 0)),
               _lesson(fri, (8, 30), (9, 15)),
               _lesson(fri, (11, 15), (12, 0))):
        cal.add_component(ev)
    return cal


def _events(monkeypatch, now: datetime, cal=None, name="Daria"):
    cal = cal or _timetable()
    monkeypatch.setattr(calendars, "_get_calendar", lambda url: cal)
    monkeypatch.setattr(calendars.db, "get_display_config", lambda: {
        "calendars": [{"name": "s", "url": "http://x", "filter": f"school={name}"}],
    })
    monkeypatch.setitem(calendars.config.WEATHER, "tz", "Europe/Berlin")
    return calendars.get_upcoming_events(now)


def test_school_hidden_before_7pm_the_day_before(monkeypatch):
    now = datetime(2026, 9, 23, 18, 59, tzinfo=TZ)
    assert _events(monkeypatch, now) == []


def test_school_groups_lessons_and_shows_full_hours_from_7pm_until_start(monkeypatch):
    for now in (datetime(2026, 9, 23, 19, 0, tzinfo=TZ),
                datetime(2026, 9, 24, 9, 14, tzinfo=TZ)):
        out = _events(monkeypatch, now)
        assert [e.summary for e in out] == ["Daria: 09:15 → 14:00"]
        assert out[0].all_day is True
        assert out[0].start == datetime(2026, 9, 24, 9, 15, tzinfo=TZ)


def test_school_shows_only_end_while_in_session(monkeypatch):
    # Between lessons still counts as "in session".
    now = datetime(2026, 9, 24, 10, 30, tzinfo=TZ)
    out = _events(monkeypatch, now)
    assert [e.summary for e in out] == ["Daria: → 14:00"]


def test_school_hidden_after_end_until_7pm_then_shows_next_day(monkeypatch):
    assert _events(monkeypatch, datetime(2026, 9, 24, 14, 0, tzinfo=TZ)) == []
    assert _events(monkeypatch, datetime(2026, 9, 24, 18, 59, tzinfo=TZ)) == []
    out = _events(monkeypatch, datetime(2026, 9, 24, 19, 0, tzinfo=TZ))
    assert [e.summary for e in out] == ["Daria: 08:30 → 12:00"]
    assert out[0].start.date() == datetime(2026, 9, 25).date()


def test_school_ignores_all_day_and_open_ended_events(monkeypatch):
    cal = icalendar.Calendar()
    all_day = icalendar.Event()
    all_day.add("SUMMARY", "Holiday")
    all_day.add("DTSTART", datetime(2026, 9, 24).date())
    no_end = icalendar.Event()
    no_end.add("SUMMARY", "Bell")
    no_end.add("DTSTART", datetime(2026, 9, 24, 8, 0, tzinfo=TZ))
    cal.add_component(all_day)
    cal.add_component(no_end)
    now = datetime(2026, 9, 23, 20, 0, tzinfo=TZ)
    assert _events(monkeypatch, now, cal=cal) == []


def test_school_name_may_be_empty(monkeypatch):
    now = datetime(2026, 9, 23, 20, 0, tzinfo=TZ)
    out = _events(monkeypatch, now, name="")
    assert out[0].summary == "09:15 → 14:00"
