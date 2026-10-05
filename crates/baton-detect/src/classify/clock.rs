//! Reset times agents print, read relative to when the screen was seen; the Python
//! detector's `clock_text` module, step for step.
//!
//! Local times are resolved like Python's `zoneinfo` with `fold=0`: a time that falls in a
//! gap or an overlap gets the offset in force before the transition (jiff's "compatible"
//! disambiguation). Two local times in the same zone are compared as wall-clock times, as
//! Python compares aware datetimes that share a `tzinfo`.

use std::sync::LazyLock;

use fancy_regex::Regex;
use jiff::Timestamp;
use jiff::civil::{Date, DateTime, Time, Weekday};
use jiff::tz::TimeZone;

use super::text::is_space;

// Hand-written patterns that compile; checked by the tests.
#[allow(clippy::expect_used)]
static CLOCK: LazyLock<Regex> = LazyLock::new(|| {
    Regex::new(r"(?i)^(\d{1,2})(?::(\d{2}))?\s*([ap]\.?m\.?)?$").expect("valid clock pattern")
});
#[allow(clippy::expect_used)]
static MONTH_DATE_TIME: LazyLock<Regex> = LazyLock::new(|| {
    Regex::new(r"^([A-Za-z]{3})[a-z]*\.?\s+(\d{1,2})(?:st|nd|rd|th)?,?\s+(\d{4}),?\s+(.+)$")
        .expect("valid date pattern")
});
#[allow(clippy::expect_used)]
static DURATION_PART: LazyLock<Regex> = LazyLock::new(|| {
    Regex::new(r"(?i)(\d+)\s*(day|hour|hr|minute|min|second|sec)s?")
        .expect("valid duration pattern")
});

const WEEKDAYS: [(&str, Weekday); 7] = [
    ("mon", Weekday::Monday),
    ("tue", Weekday::Tuesday),
    ("wed", Weekday::Wednesday),
    ("thu", Weekday::Thursday),
    ("fri", Weekday::Friday),
    ("sat", Weekday::Saturday),
    ("sun", Weekday::Sunday),
];
const MONTHS: [&str; 12] = [
    "jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec",
];

fn strip(text: &str) -> &str {
    text.trim_matches(is_space)
}

fn number(text: &str) -> Option<i64> {
    text.parse().ok()
}

/// `3:45pm`, `3pm`, `12:00am`, `15:45` → time of day.
#[must_use]
pub fn parse_clock(text: &str) -> Option<Time> {
    let caps = CLOCK.captures(strip(text)).ok()??;
    let mut hour = number(caps.get(1)?.as_str())?;
    let minute = caps.get(2).map_or(Some(0), |m| number(m.as_str()))?;
    let meridiem = caps
        .get(3)
        .map(|m| m.as_str().to_lowercase().replace('.', ""))
        .unwrap_or_default();
    if !meridiem.is_empty() {
        if !(1..=12).contains(&hour) {
            return None;
        }
        hour = hour % 12 + if meridiem == "pm" { 12 } else { 0 };
    }
    if hour > 23 || minute > 59 {
        return None;
    }
    Time::new(i8::try_from(hour).ok()?, i8::try_from(minute).ok()?, 0, 0).ok()
}

fn resolve(local: DateTime, zone: &TimeZone) -> Option<Timestamp> {
    zone.to_ambiguous_timestamp(local).compatible().ok()
}

/// The first moment after `after` showing `text` on a clock in `zone`, optionally on a
/// given weekday (`Mon`, `Tuesday`, …).
#[must_use]
pub fn next_clock_time(
    text: &str,
    after: Timestamp,
    zone: &TimeZone,
    weekday: Option<&str>,
) -> Option<Timestamp> {
    let clock = parse_clock(text)?;
    let now = zone.to_datetime(after);
    let mut candidate = now.date().to_datetime(clock);
    if let Some(name) = weekday {
        let key: String = strip(name).to_lowercase().chars().take(3).collect();
        let target = WEEKDAYS.iter().find(|(k, _)| *k == key)?.1;
        let ahead =
            (target.to_monday_zero_offset() - now.weekday().to_monday_zero_offset()).rem_euclid(7);
        candidate = add_days(candidate, i64::from(ahead))?;
        if candidate <= now {
            candidate = add_days(candidate, 7)?;
        }
    } else if candidate <= now {
        candidate = add_days(candidate, 1)?;
    }
    resolve(candidate, zone)
}

fn add_days(at: DateTime, days: i64) -> Option<DateTime> {
    at.checked_add(jiff::Span::new().days(days)).ok()
}

/// `Feb 23rd, 2026 9:01 PM`, a local time in `zone`.
#[must_use]
pub fn parse_month_date_time(text: &str, zone: &TimeZone) -> Option<Timestamp> {
    let caps = MONTH_DATE_TIME.captures(strip(text)).ok()??;
    let month_name = caps.get(1)?.as_str().to_lowercase();
    let clock = parse_clock(caps.get(4)?.as_str())?;
    let month = MONTHS.iter().position(|m| *m == month_name)?;
    // Python's dates start at year 1; jiff's go further back.
    let year = number(caps.get(3)?.as_str()).filter(|&y| y >= 1)?;
    let date = Date::new(
        i16::try_from(year).ok()?,
        i8::try_from(month + 1).ok()?,
        i8::try_from(number(caps.get(2)?.as_str())?).ok()?,
    )
    .ok()?;
    resolve(date.to_datetime(clock), zone)
}

/// `3 hours 2 minutes`, `4 days 2 hours 46 minutes` → seconds.
#[must_use]
pub fn parse_duration(text: &str) -> Option<i64> {
    let mut seconds: i64 = 0;
    let mut found = false;
    for caps in DURATION_PART.captures_iter(text) {
        let caps = caps.ok()?;
        found = true;
        let count = number(caps.get(1)?.as_str())?;
        let unit = match caps.get(2)?.as_str().to_lowercase().as_str() {
            "day" => 86_400,
            "hour" | "hr" => 3_600,
            "minute" | "min" => 60,
            _ => 1,
        };
        seconds = seconds.checked_add(count.checked_mul(unit)?)?;
    }
    found.then_some(seconds)
}

#[cfg(test)]
mod tests {
    #![allow(clippy::unwrap_used)] // a failure should stop the test, with the error

    use super::*;

    fn at(text: &str) -> Timestamp {
        text.parse().unwrap()
    }

    fn zone(name: &str) -> TimeZone {
        TimeZone::get(name).unwrap()
    }

    #[test]
    fn clocks() {
        assert_eq!(parse_clock("3:45pm"), Some(Time::constant(15, 45, 0, 0)));
        assert_eq!(parse_clock(" 12am "), Some(Time::constant(0, 0, 0, 0)));
        assert_eq!(
            parse_clock("12:30 p.m."),
            Some(Time::constant(12, 30, 0, 0))
        );
        assert_eq!(parse_clock("15:45"), Some(Time::constant(15, 45, 0, 0)));
        assert_eq!(parse_clock("13pm"), None);
        assert_eq!(parse_clock("24:00"), None);
    }

    #[test]
    fn next_clock_times() {
        let ist = zone("Europe/Istanbul");
        let now = at("2026-10-01T11:00:00Z"); // Thursday 14:00 in Istanbul
        assert_eq!(
            next_clock_time("3:45pm", now, &ist, None),
            Some(at("2026-10-01T12:45:00Z"))
        );
        assert_eq!(
            next_clock_time("1pm", now, &ist, None),
            Some(at("2026-10-02T10:00:00Z"))
        );
        assert_eq!(
            next_clock_time("12:00am", now, &ist, Some("Mon")),
            Some(at("2026-10-04T21:00:00Z"))
        );
        assert_eq!(next_clock_time("3pm", now, &ist, Some("Xyz")), None);
    }

    #[test]
    fn a_time_in_a_spring_gap_takes_the_offset_before_it() {
        // 2026-03-29 02:30 does not exist in Berlin; Python's fold=0 reads it as +01:00.
        let berlin = zone("Europe/Berlin");
        assert_eq!(
            next_clock_time("2:30am", at("2026-03-28T23:00:00Z"), &berlin, None),
            Some(at("2026-03-29T01:30:00Z"))
        );
    }

    #[test]
    fn dates_and_durations() {
        assert_eq!(
            parse_month_date_time("Feb 23rd, 2026 9:01 PM", &zone("Europe/Istanbul")),
            Some(at("2026-02-23T18:01:00Z"))
        );
        assert_eq!(
            parse_month_date_time("Foo 1, 2026 9:01 PM", &TimeZone::UTC),
            None
        );
        assert_eq!(parse_duration("3 hours 2 minutes"), Some(3 * 3600 + 120));
        assert_eq!(
            parse_duration("4 days 2 hrs 46 min"),
            Some(4 * 86400 + 7200 + 2760)
        );
        assert_eq!(parse_duration("soon"), None);
    }
}
