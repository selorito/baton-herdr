from __future__ import annotations

from datetime import UTC, datetime, time, timedelta
from zoneinfo import ZoneInfo

import pytest
from hypothesis import given
from hypothesis import strategies as st

from coban.core.clock_text import (
    next_clock_time,
    parse_clock,
    parse_duration,
    parse_month_date_time,
)

ISTANBUL = ZoneInfo("Europe/Istanbul")  # UTC+3, no DST
# Thursday 2026-10-01 14:00 in Istanbul.
AFTER = datetime(2026, 10, 1, 11, 0, tzinfo=UTC)


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("3:45pm", time(15, 45)),
        ("3pm", time(15, 0)),
        ("12:00am", time(0, 0)),
        ("12:30pm", time(12, 30)),
        ("9:01 PM", time(21, 1)),
        ("15:45", time(15, 45)),
        ("13pm", None),
        ("25:00", None),
        ("soon", None),
    ],
)
def test_parse_clock(text: str, expected: time | None) -> None:
    assert parse_clock(text) == expected


def test_a_later_time_today_is_today_and_an_earlier_one_is_tomorrow() -> None:
    assert next_clock_time("3:45pm", after=AFTER, zone=ISTANBUL) == datetime(
        2026, 10, 1, 12, 45, tzinfo=UTC
    )
    assert next_clock_time("1pm", after=AFTER, zone=ISTANBUL) == datetime(
        2026, 10, 2, 10, 0, tzinfo=UTC
    )


def test_a_weekday_points_at_the_next_such_day() -> None:
    # Monday midnight in Istanbul is Sunday 21:00 UTC.
    assert next_clock_time("12:00am", after=AFTER, zone=ISTANBUL, weekday="Mon") == datetime(
        2026, 10, 4, 21, 0, tzinfo=UTC
    )
    # Same weekday, time already passed: a week later.
    assert next_clock_time("1pm", after=AFTER, zone=ISTANBUL, weekday="Thu") == datetime(
        2026, 10, 8, 10, 0, tzinfo=UTC
    )
    assert next_clock_time("1pm", after=AFTER, zone=ISTANBUL, weekday="Someday") is None


@given(
    after=st.datetimes(
        # Hypothesis wants naive bounds and adds the zone itself.
        min_value=datetime(2020, 1, 1),  # noqa: DTZ001
        max_value=datetime(2035, 1, 1),  # noqa: DTZ001
        timezones=st.just(UTC),
    ),
    hour=st.integers(min_value=0, max_value=23),
    minute=st.integers(min_value=0, max_value=59),
    zone=st.sampled_from([ZoneInfo("UTC"), ISTANBUL, ZoneInfo("America/New_York")]),
)
def test_next_clock_time_is_always_in_the_following_day(
    after: datetime, hour: int, minute: int, zone: ZoneInfo
) -> None:
    result = next_clock_time(f"{hour}:{minute:02d}", after=after, zone=zone)
    assert result is not None
    assert after < result <= after + timedelta(days=1, hours=1)  # one DST hour of slack


def test_parse_month_date_time_reads_codex_absolute_times_as_local() -> None:
    assert parse_month_date_time("Feb 23rd, 2026 9:01 PM", zone=ISTANBUL) == datetime(
        2026, 2, 23, 18, 1, tzinfo=UTC
    )
    assert parse_month_date_time("Apr 12th, 2026 3:31 PM", zone=ZoneInfo("UTC")) == datetime(
        2026, 4, 12, 15, 31, tzinfo=UTC
    )
    assert parse_month_date_time("Feb 30th, 2026 9:01 PM", zone=ISTANBUL) is None


def test_parse_duration() -> None:
    assert parse_duration("3 hours 2 minutes") == timedelta(hours=3, minutes=2)
    assert parse_duration("4 days 2 hours 46 minutes") == timedelta(days=4, hours=2, minutes=46)
    assert parse_duration("a while") is None
