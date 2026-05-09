"""Pull and filter upcoming events from a fixed list of .ics URLs.

Three filter modes (config.CALENDARS[i]["filter"]):
  * "all"               — show every occurrence in the horizon
  * "attendee=<email>"  — keep occurrences where <email> is on ATTENDEE
  * "trashcan"          — synthesise "take out" / "bring back" pseudo-events
                          based on the current time vs each occurrence

Each calendar's parsed result is cached for CALENDAR_TTL_SECONDS; refreshes
that fail leave the previous cache value in place (stale-on-error).
"""

import logging
import time
import urllib.request
from dataclasses import dataclass
from datetime import date, datetime, time as time_obj, timedelta
from typing import Optional
from zoneinfo import ZoneInfo

import icalendar
import recurring_ical_events

import config
import db

log = logging.getLogger(__name__)

# url -> (fetched_at_monotonic, parsed_Calendar_or_None)
_calendar_cache: dict[str, tuple[float, Optional[icalendar.Calendar]]] = {}


@dataclass(frozen=True)
class CalendarEvent:
    start: datetime
    summary: str
    end: Optional[datetime] = None
    all_day: bool = False


def get_upcoming_events(now: datetime) -> list[CalendarEvent]:
    """Up to EVENTS_TO_SHOW events, sorted ascending by start time.

    `now` should be tz-aware; we re-anchor to the configured display tz
    before comparing windows so trashcan logic uses local wall-clock time.
    """
    tz = ZoneInfo(config.WEATHER["tz"])
    now = now.astimezone(tz)
    horizon = now + timedelta(days=config.EVENT_HORIZON_DAYS)
    out: list[CalendarEvent] = []

    cal_list = db.get_display_config().get("calendars", [])
    for entry in cal_list:
        url = entry.get("url", "").strip()
        if not url:
            continue

        cal = _get_calendar(url)
        if cal is None:
            continue

        flt = entry.get("filter", "all")
        # Look 12h into the past for trashcan calendars so we still see
        # this morning's pickup while the bring-back window is open.
        window_start = now - timedelta(hours=12) if flt == "trashcan" else now

        try:
            occurrences = recurring_ical_events.of(cal).between(window_start, horizon)
        except Exception as e:
            log.warning(f"calendar {entry['name']}: expand failed: {e}")
            continue

        for occ in occurrences:
            out.extend(_apply_filter(occ, flt, now, tz))

    out.sort(key=lambda e: e.start)
    return out[: config.EVENTS_TO_SHOW]


def _get_calendar(url: str) -> Optional[icalendar.Calendar]:
    now = time.monotonic()
    cached = _calendar_cache.get(url)
    if cached and (now - cached[0]) < config.CALENDAR_TTL_SECONDS:
        return cached[1]

    # Some iCal hosts (e.g. mijnafvalwijzer) 403 the default Python-urllib UA.
    req = urllib.request.Request(url, headers={"User-Agent": "talker-display/1.0"})
    try:
        # 10s — Google Calendar's iCal endpoint occasionally takes >5s
        # from Cloud Run egress; a single missed refresh costs us a calendar.
        with urllib.request.urlopen(req, timeout=10) as resp:
            ctype = resp.headers.get("Content-Type", "").lower()
            if "text/calendar" not in ctype and "text/plain" not in ctype:
                log.warning(
                    f"calendar fetch: not an iCal feed (Content-Type={ctype!r}) "
                    f"for {url} — looks like a calendar UI URL, not the "
                    f"'Secret address in iCal format'.")
                return cached[1] if cached else None
            cal = icalendar.Calendar.from_ical(resp.read())
    except Exception as e:
        # Truncate so a megabyte of HTML in a parse error doesn't flood logs.
        msg = repr(e)
        if len(msg) > 200:
            msg = msg[:200] + "…"
        log.warning(f"calendar fetch failed for {url}: {msg}")
        return cached[1] if cached else None

    _calendar_cache[url] = (now, cal)
    return cal


def _apply_filter(occ, filter_str: str, now: datetime,
                  tz: ZoneInfo) -> list[CalendarEvent]:
    start, all_day = _occ_start(occ, tz)
    if start is None:
        return []
    end = None if all_day else _occ_end(occ, tz, start)
    summary = str(occ.get("SUMMARY", "")).strip()

    if filter_str == "trashcan":
        return _synthesize_trashcan(start, all_day, now, tz)

    # Future-only for non-trashcan filters.
    if start < now:
        return []

    if filter_str == "all":
        return [CalendarEvent(start=start, end=end, summary=summary,
                              all_day=all_day)]

    if filter_str.startswith("attendee="):
        wanted = filter_str.split("=", 1)[1].strip().lower()
        if _has_attendee(occ, wanted):
            return [CalendarEvent(start=start, end=end, summary=summary,
                                  all_day=all_day)]
        return []

    log.warning(f"unknown calendar filter: {filter_str!r}")
    return []


def _occ_start(occ, tz: ZoneInfo) -> tuple[Optional[datetime], bool]:
    dtstart = occ.get("DTSTART")
    if dtstart is None:
        return None, False
    val = dtstart.dt
    # datetime is a subclass of date — check it first.
    if isinstance(val, datetime):
        start = val.astimezone(tz) if val.tzinfo else val.replace(tzinfo=tz)
        return start, False
    if isinstance(val, date):
        return datetime.combine(val, time_obj.min, tzinfo=tz), True
    return None, False


def _occ_end(occ, tz: ZoneInfo, start: datetime) -> Optional[datetime]:
    """End time for a non-all-day VEVENT. Honors DTEND first, falls back
    to DTSTART + DURATION. Returns None if neither is present (instantaneous
    event)."""
    dtend = occ.get("DTEND")
    if dtend is not None:
        val = dtend.dt
        if isinstance(val, datetime):
            return val.astimezone(tz) if val.tzinfo else val.replace(tzinfo=tz)
        if isinstance(val, date):
            return datetime.combine(val, time_obj.min, tzinfo=tz)
    duration = occ.get("DURATION")
    if duration is not None:
        try:
            return start + duration.dt
        except Exception:
            pass
    return None


def _has_attendee(occ, wanted_email: str) -> bool:
    attendees = occ.get("ATTENDEE")
    if attendees is None:
        return False
    if not isinstance(attendees, list):
        attendees = [attendees]
    for a in attendees:
        s = str(a).lower().strip()
        if s.startswith("mailto:"):
            s = s[len("mailto:"):]
        if s == wanted_email:
            return True
    return False


def _synthesize_trashcan(start: datetime, all_day: bool, now: datetime,
                         tz: ZoneInfo) -> list[CalendarEvent]:
    event_day = start.astimezone(tz).date()
    take_out_from = datetime.combine(
        event_day - timedelta(days=1), time_obj(19, 0), tzinfo=tz)
    bring_back_until = datetime.combine(
        event_day, time_obj(12, 0), tzinfo=tz)

    if take_out_from <= now < start:
        return [CalendarEvent(start=start, summary="take out", all_day=all_day)]
    if start <= now < bring_back_until:
        return [CalendarEvent(start=start, summary="bring back", all_day=all_day)]
    return []
