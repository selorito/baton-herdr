//! Claude Code's own rate limits, from its status line (ADR 0015).
//!
//! Claude Code reports its 5-hour and 7-day windows only to a status line command, as
//! `rate_limits.five_hour` / `rate_limits.seven_day` with `used_percentage` (0-100) and
//! `resets_at` (Unix seconds), for Pro and Max plans and after the session's first
//! response (code.claude.com/docs/en/statusline). `baton-detect statusline` runs as that
//! command in the Claude sessions baton starts: [`record`] keeps the session id and the
//! windows, with the time they were seen, as one line of a log; the collector reads the
//! log back with [`parse_line`]. Nothing else of the payload (paths, model, cost) is kept.

use serde::Deserialize;
use serde_json::{Value, json};
use time::OffsetDateTime;

use super::{
    Agent, ParseError, RateLimitObservation, RateLimitWindow, format_time, from_unix, parse_json,
    parse_time,
};

/// The windows Claude Code reports, with their length in minutes.
const WINDOWS: [(&str, u64); 2] = [("five_hour", 5 * 60), ("seven_day", 7 * 24 * 60)];

/// The log line for a status line payload, or `None` when it carries no window.
#[must_use]
pub fn record(payload: &Value, now: OffsetDateTime) -> Option<String> {
    let limits = payload.get("rate_limits")?;
    let mut kept = serde_json::Map::new();
    for (name, _) in WINDOWS {
        if let Some(window) = limits.get(name).filter(|w| w.is_object()) {
            kept.insert(name.to_owned(), window.clone());
        }
    }
    if kept.is_empty() {
        return None;
    }
    let session = payload.get("session_id").and_then(Value::as_str);
    Some(
        json!({
            "at": format_time(now),
            "session_id": session,
            "rate_limits": kept,
        })
        .to_string(),
    )
}

/// Whether two log lines report the same windows, whatever their times.
#[must_use]
pub fn same_windows(a: &str, b: &str) -> bool {
    let windows = |line: &str| {
        serde_json::from_str::<Value>(line)
            .ok()
            .and_then(|v| v.get("rate_limits").cloned())
    };
    windows(a).is_some_and(|w| Some(w) == windows(b))
}

#[derive(Debug, Deserialize)]
struct Line {
    at: String,
    session_id: Option<String>,
    rate_limits: std::collections::HashMap<String, Window>,
}

#[derive(Debug, Deserialize)]
struct Window {
    used_percentage: Option<f64>,
    resets_at: Option<i64>,
}

/// A rate-limit observation from one log line written by [`record`].
///
/// # Errors
/// When the line is not such a record.
pub fn parse_line(line: &str) -> Result<Option<RateLimitObservation>, ParseError> {
    let line: Line = parse_json(line)?;
    let at = parse_time(&line.at)?;
    let windows: Vec<RateLimitWindow> = WINDOWS
        .iter()
        .filter_map(|(name, minutes)| {
            let window = line.rate_limits.get(*name)?;
            Some(RateLimitWindow {
                name: (*name).to_owned(),
                window_minutes: *minutes,
                used_percent: window.used_percentage?,
                resets_at: window.resets_at.and_then(from_unix),
            })
        })
        .collect();
    if windows.is_empty() {
        return Ok(None);
    }
    Ok(Some(RateLimitObservation {
        agent: Agent::Claude,
        session_id: line.session_id.unwrap_or_default(),
        at,
        windows,
    }))
}

/// What the status line shows: `5h 23% · 7d 41%`, or nothing before the first report.
///
/// Plain words and digits only, so the line can never look like a screen the classifier
/// reads (a spinner, a prompt, a limit message).
#[must_use]
pub fn status_text(payload: &Value) -> String {
    let Some(limits) = payload.get("rate_limits") else {
        return String::new();
    };
    [("five_hour", "5h"), ("seven_day", "7d")]
        .iter()
        .filter_map(|(name, label)| {
            let used = limits.get(*name)?.get("used_percentage")?.as_f64()?;
            Some(format!("{label} {used:.0}%"))
        })
        .collect::<Vec<_>>()
        .join(" · ")
}

#[cfg(test)]
mod tests {
    use super::*;

    fn payload() -> Value {
        // The shape documented at code.claude.com/docs/en/statusline.
        json!({
            "session_id": "abc123",
            "transcript_path": "/home/someone/.claude/projects/x/abc123.jsonl",
            "model": {"display_name": "Opus"},
            "cost": {"total_cost_usd": 0.01},
            "rate_limits": {
                "five_hour": {"used_percentage": 23.5, "resets_at": 1_738_425_600},
                "seven_day": {"used_percentage": 41.2, "resets_at": 1_738_857_600}
            }
        })
    }

    #[test]
    fn a_payload_is_kept_as_its_session_and_windows_only() {
        let at = from_unix(1_738_400_000).unwrap();
        let line = record(&payload(), at).unwrap();
        assert!(!line.contains("transcript_path") && !line.contains("cost"));

        let observation = parse_line(&line).unwrap().unwrap();
        assert_eq!(observation.agent, Agent::Claude);
        assert_eq!(observation.session_id, "abc123");
        assert_eq!(observation.at, at);
        let windows: Vec<_> = observation
            .windows
            .iter()
            .map(|w| (w.name.as_str(), w.window_minutes, w.used_percent))
            .collect();
        assert_eq!(
            windows,
            [("five_hour", 300, 23.5), ("seven_day", 10_080, 41.2)]
        );
        assert_eq!(observation.windows[0].resets_at, from_unix(1_738_425_600));
    }

    #[test]
    fn without_windows_nothing_is_recorded_or_shown() {
        // Before the first response, and for API-key users, there are no rate limits.
        let empty = json!({"session_id": "s", "model": {"display_name": "Opus"}});
        assert_eq!(record(&empty, from_unix(0).unwrap()), None);
        assert_eq!(status_text(&empty), "");
        let spend_only = json!({"rate_limits": {"spend_limit": {"used_percentage": 5}}});
        assert_eq!(record(&spend_only, from_unix(0).unwrap()), None);
    }

    #[test]
    fn the_status_text_shows_both_windows_rounded() {
        assert_eq!(status_text(&payload()), "5h 24% · 7d 41%");
        let five_only = json!({"rate_limits": {"five_hour": {"used_percentage": 3}}});
        assert_eq!(status_text(&five_only), "5h 3%");
    }

    #[test]
    fn repeats_are_told_apart_by_their_windows_not_their_times() {
        let first = record(&payload(), from_unix(0).unwrap()).unwrap();
        let later = record(&payload(), from_unix(60).unwrap()).unwrap();
        assert!(same_windows(&first, &later));
        let mut changed = payload();
        changed["rate_limits"]["five_hour"]["used_percentage"] = json!(30);
        let moved = record(&changed, from_unix(60).unwrap()).unwrap();
        assert!(!same_windows(&first, &moved));
        assert!(!same_windows(&first, "not json"));
    }

    #[test]
    fn a_window_without_a_share_is_left_out() {
        let line = r#"{"at":"2026-10-06T10:00:00.000Z","session_id":null,"rate_limits":{"five_hour":{"resets_at":1}}}"#;
        assert!(parse_line(line).unwrap().is_none());
        assert!(parse_line("{}").is_err());
    }
}
