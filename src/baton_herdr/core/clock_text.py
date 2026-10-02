"""Read the reset times agents print, relative to when the screen was seen.

Agents print times without a date ("resets 3:45pm"), with a weekday
("resets Mon 12:00am"), as a full local date ("try again at Feb 23rd, 2026
9:01 PM") or as a duration ("try again in 3 hours 2 minutes"). Each function
returns an aware UTC datetime, or ``None`` when the text cannot be read; a
reset time is advice, never a reason to fail.
"""

from __future__ import annotations

import re
from datetime import UTC, datetime, time, timedelta
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from zoneinfo import ZoneInfo

_CLOCK = re.compile(r"^(\d{1,2})(?::(\d{2}))?\s*([ap]\.?m\.?)?$", re.IGNORECASE)
_WEEKDAYS = ("mon", "tue", "wed", "thu", "fri", "sat", "sun")
_MONTHS = ("jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec")
_DURATION_PART = re.compile(r"(\d+)\s*(day|hour|hr|minute|min|second|sec)s?", re.IGNORECASE)


def parse_clock(text: str) -> time | None:
    """``3:45pm``, ``3pm``, ``12:00am``, ``15:45`` → time of day."""
    match = _CLOCK.match(text.strip())
    if match is None:
        return None
    hour, minute = int(match.group(1)), int(match.group(2) or 0)
    meridiem = (match.group(3) or "").lower().replace(".", "")
    if meridiem:
        if not 1 <= hour <= 12:
            return None
        hour = hour % 12 + (12 if meridiem == "pm" else 0)
    if hour > 23 or minute > 59:
        return None
    return time(hour, minute)


def next_clock_time(
    text: str, *, after: datetime, zone: ZoneInfo, weekday: str | None = None
) -> datetime | None:
    """The first moment after ``after`` showing ``text`` on a clock in ``zone``."""
    clock = parse_clock(text)
    if clock is None:
        return None
    local_now = after.astimezone(zone)
    candidate = datetime.combine(local_now.date(), clock, tzinfo=zone)
    if weekday is not None:
        try:
            target = _WEEKDAYS.index(weekday.strip().lower()[:3])
        except ValueError:
            return None
        candidate += timedelta(days=(target - local_now.weekday()) % 7)
        if candidate <= local_now:
            candidate += timedelta(days=7)
    elif candidate <= local_now:
        candidate += timedelta(days=1)
    return candidate.astimezone(UTC)


def parse_month_date_time(text: str, *, zone: ZoneInfo) -> datetime | None:
    """``Feb 23rd, 2026 9:01 PM`` (local time in ``zone``) → aware UTC datetime."""
    match = re.match(
        r"^([A-Za-z]{3})[a-z]*\.?\s+(\d{1,2})(?:st|nd|rd|th)?,?\s+(\d{4}),?\s+(.+)$", text.strip()
    )
    if match is None:
        return None
    month_name, day, year, clock_text = match.groups()
    clock = parse_clock(clock_text)
    if clock is None or month_name.lower() not in _MONTHS:
        return None
    try:
        local = datetime(
            int(year),
            _MONTHS.index(month_name.lower()) + 1,
            int(day),
            clock.hour,
            clock.minute,
            tzinfo=zone,
        )
    except ValueError:
        return None
    return local.astimezone(UTC)


def parse_duration(text: str) -> timedelta | None:
    """``3 hours 2 minutes``, ``4 days 2 hours 46 minutes`` → timedelta."""
    parts = _DURATION_PART.findall(text)
    if not parts:
        return None
    units = {"day": 86400, "hour": 3600, "hr": 3600, "minute": 60, "min": 60, "second": 1, "sec": 1}
    return timedelta(seconds=sum(int(n) * units[unit.lower()] for n, unit in parts))
