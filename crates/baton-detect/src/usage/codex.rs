//! Codex CLI rollouts: `~/.codex/sessions/YYYY/MM/DD/rollout-*.jsonl`.
//!
//! Codex reports usage as `event_msg` / `token_count` events whose
//! `info.total_token_usage` is cumulative for the session; a usage record is the
//! difference to the previous event. `codex resume` appends to the same file, so the
//! running total carries over a resume. If the counter starts again from zero (a field
//! goes down, or the total equals the last response's usage while the difference does
//! not), the new total itself is the usage. The newer per-response
//! `token_usage_record` lines repeat the same tokens and are ignored, so nothing is
//! counted twice.
//!
//! The same events carry `rate_limits` (the 5-hour and 7-day windows); an observation
//! is reported whenever it differs from the last one in this file.

use serde::Deserialize;
use time::OffsetDateTime;

use super::{
    Agent, Event, ParseError, RateLimitObservation, RateLimitWindow, Tokens, UsageRecord,
    format_time, from_unix, parse_time,
};

#[derive(Debug, Deserialize)]
struct Line {
    timestamp: Option<String>,
    #[serde(rename = "type")]
    kind: Option<String>,
    payload: Option<serde_json::Value>,
}

#[derive(Debug, Deserialize)]
struct SessionMeta {
    id: Option<String>,
    session_id: Option<String>,
}

#[derive(Debug, Deserialize)]
struct TurnContext {
    model: Option<String>,
}

#[derive(Debug, Deserialize)]
struct TokenCount {
    #[serde(rename = "type")]
    kind: Option<String>,
    info: Option<TokenInfo>,
    rate_limits: Option<RateLimits>,
}

#[derive(Debug, Deserialize)]
struct TokenInfo {
    total_token_usage: Option<Totals>,
    last_token_usage: Option<Totals>,
}

/// Codex's counters. `cached_input_tokens` is part of `input_tokens`, and
/// `reasoning_output_tokens` part of `output_tokens`.
#[allow(clippy::struct_field_names)] // Codex's field names
#[derive(Debug, Clone, Copy, PartialEq, Eq, Default, Deserialize)]
struct Totals {
    #[serde(default)]
    input_tokens: u64,
    #[serde(default)]
    cached_input_tokens: u64,
    #[serde(default)]
    cache_write_input_tokens: u64,
    #[serde(default)]
    output_tokens: u64,
    #[serde(default)]
    reasoning_output_tokens: u64,
    #[serde(default)]
    total_tokens: u64,
}

impl Totals {
    fn fields(self) -> [u64; 6] {
        [
            self.input_tokens,
            self.cached_input_tokens,
            self.cache_write_input_tokens,
            self.output_tokens,
            self.reasoning_output_tokens,
            self.total_tokens,
        ]
    }

    /// `self - earlier`, or `None` if any counter went down.
    fn minus(self, earlier: Totals) -> Option<Totals> {
        Some(Totals {
            input_tokens: self.input_tokens.checked_sub(earlier.input_tokens)?,
            cached_input_tokens: self
                .cached_input_tokens
                .checked_sub(earlier.cached_input_tokens)?,
            cache_write_input_tokens: self
                .cache_write_input_tokens
                .checked_sub(earlier.cache_write_input_tokens)?,
            output_tokens: self.output_tokens.checked_sub(earlier.output_tokens)?,
            reasoning_output_tokens: self
                .reasoning_output_tokens
                .checked_sub(earlier.reasoning_output_tokens)?,
            total_tokens: self.total_tokens.checked_sub(earlier.total_tokens)?,
        })
    }

    fn is_zero(self) -> bool {
        self.fields().iter().all(|&n| n == 0)
    }

    fn tokens(self) -> Tokens {
        Tokens {
            input: self.input_tokens.saturating_sub(self.cached_input_tokens),
            output: self.output_tokens,
            cache_read: self.cached_input_tokens,
            cache_write: self.cache_write_input_tokens,
            reasoning: Some(self.reasoning_output_tokens),
        }
    }
}

#[derive(Debug, Deserialize)]
struct RateLimits {
    primary: Option<LimitWindow>,
    secondary: Option<LimitWindow>,
}

#[derive(Debug, Deserialize)]
struct LimitWindow {
    used_percent: Option<f64>,
    window_minutes: Option<u64>,
    /// Unix seconds (current Codex).
    resets_at: Option<i64>,
    /// Seconds from the event (older Codex).
    resets_in_seconds: Option<i64>,
}

/// Reads one rollout file, line by line, keeping the running total.
#[derive(Debug, Default)]
pub struct CodexLog {
    session: Option<String>,
    model: Option<String>,
    previous: Option<Totals>,
    last_limits: Option<Vec<RateLimitWindow>>,
}

impl CodexLog {
    /// The usage record and the rate-limit observation `line` yields, if any.
    ///
    /// # Errors
    /// A line that is not JSON, or a `token_count` before the session is known or
    /// without a valid timestamp. The caller reports it and goes on.
    pub fn parse_line(&mut self, line: &str) -> Result<Vec<Event>, ParseError> {
        let line: Line = serde_json::from_str(line)?;
        let Some(payload) = line.payload else {
            return Ok(Vec::new());
        };
        match line.kind.as_deref() {
            Some("session_meta") => {
                let meta: SessionMeta = serde_json::from_value(payload)?;
                // A resumed session writes its meta again: same id, totals carry on.
                if let Some(id) = meta.id.or(meta.session_id) {
                    self.session = Some(id);
                }
                Ok(Vec::new())
            }
            Some("turn_context") => {
                let context: TurnContext = serde_json::from_value(payload)?;
                if context.model.is_some() {
                    self.model = context.model;
                }
                Ok(Vec::new())
            }
            Some("event_msg") => {
                let count: TokenCount = serde_json::from_value(payload)?;
                if count.kind.as_deref() != Some("token_count") {
                    return Ok(Vec::new());
                }
                let session = self
                    .session
                    .clone()
                    .ok_or(ParseError::Missing("session_meta"))?;
                let at = parse_time(&line.timestamp.ok_or(ParseError::Missing("timestamp"))?)?;
                let mut events = Vec::new();
                if let Some(record) = self.usage(&session, at, count.info) {
                    events.push(Event::Usage(record));
                }
                if let Some(limits) = self.limits(&session, at, count.rate_limits) {
                    events.push(Event::RateLimits(limits));
                }
                Ok(events)
            }
            _ => Ok(Vec::new()),
        }
    }

    fn usage(
        &mut self,
        session: &str,
        at: OffsetDateTime,
        info: Option<TokenInfo>,
    ) -> Option<UsageRecord> {
        let info = info?;
        let total = info.total_token_usage?;
        let delta = match (self.previous, info.last_token_usage) {
            (None, _) => total,
            (Some(previous), last) => match total.minus(previous) {
                // Restarted from zero, though every counter grew past the old total.
                Some(delta) if last.is_some_and(|l| l == total && l != delta) => total,
                Some(delta) => delta,
                None => total, // a counter went down: restarted from zero
            },
        };
        self.previous = Some(total);
        if delta.is_zero() {
            return None; // the same count reported again
        }
        Some(UsageRecord {
            agent: Agent::Codex,
            session_id: session.to_owned(),
            record_id: format!("codex:{session}:{}:{}", format_time(at), total.total_tokens),
            at,
            model: self.model.clone(),
            tokens: delta.tokens(),
        })
    }

    fn limits(
        &mut self,
        session: &str,
        at: OffsetDateTime,
        limits: Option<RateLimits>,
    ) -> Option<RateLimitObservation> {
        let limits = limits?;
        let windows: Vec<RateLimitWindow> =
            [("primary", limits.primary), ("secondary", limits.secondary)]
                .into_iter()
                .filter_map(|(name, window)| window_of(name, window.as_ref()?, at))
                .collect();
        if windows.is_empty() || self.last_limits.as_ref() == Some(&windows) {
            return None;
        }
        self.last_limits = Some(windows.clone());
        Some(RateLimitObservation {
            agent: Agent::Codex,
            session_id: session.to_owned(),
            at,
            windows,
        })
    }
}

fn window_of(name: &str, window: &LimitWindow, at: OffsetDateTime) -> Option<RateLimitWindow> {
    let resets_at = match (window.resets_at, window.resets_in_seconds) {
        (Some(seconds), _) => from_unix(seconds),
        (None, Some(seconds)) => Some(at + time::Duration::seconds(seconds)),
        (None, None) => None,
    };
    Some(RateLimitWindow {
        name: name.to_owned(),
        window_minutes: window.window_minutes?,
        used_percent: window.used_percent?,
        resets_at,
    })
}

#[cfg(test)]
mod tests {
    use super::*;

    const RESUME: &str = include_str!("../../tests/data/codex-resume.jsonl");

    fn events(text: &str) -> Vec<Event> {
        let mut log = CodexLog::default();
        text.lines()
            .filter(|l| !l.trim().is_empty())
            .flat_map(|line| log.parse_line(line).unwrap())
            .collect()
    }

    fn usage(events: &[Event]) -> Vec<&UsageRecord> {
        events
            .iter()
            .filter_map(|e| match e {
                Event::Usage(record) => Some(record),
                Event::RateLimits(_) => None,
            })
            .collect()
    }

    #[test]
    fn cumulative_counts_become_per_response_usage_across_a_resume() {
        let all = events(RESUME);
        let records = usage(&all);
        let totals: Vec<u64> = records
            .iter()
            .map(|r| r.tokens.input + r.tokens.cache_read + r.tokens.cache_write + r.tokens.output)
            .collect();
        // 1000, +500, (same count again: nothing), resume, +300, restart from 0: 400,
        // then 1000 again as a cumulative value, at another time: +600.
        assert_eq!(totals, [1000, 500, 300, 400, 600]);
        let first = records[0];
        assert_eq!(first.session_id, "c-1");
        assert_eq!(first.model.as_deref(), Some("gpt-5.6-luna"));
        assert_eq!(
            first.tokens,
            Tokens {
                input: 200,
                output: 100,
                cache_read: 700,
                cache_write: 0,
                reasoning: Some(30)
            }
        );
        // The ignored token_usage_record lines would have doubled the first response.
        assert_eq!(records.len(), 5);
    }

    #[test]
    fn a_cumulative_value_seen_again_after_a_restart_keeps_its_own_record() {
        let all = events(RESUME);
        let ids: Vec<&str> = usage(&all).iter().map(|r| r.record_id.as_str()).collect();
        let at_1000: Vec<&&str> = ids.iter().filter(|id| id.ends_with(":1000")).collect();
        assert_eq!(at_1000.len(), 2, "{ids:?}");
        assert_ne!(at_1000[0], at_1000[1]);
        assert_eq!(*at_1000[0], "codex:c-1:2026-10-01T10:00:10.000Z:1000");
    }

    #[test]
    fn rate_limits_are_reported_when_they_change() {
        let all = events(RESUME);
        let limits: Vec<&RateLimitObservation> = all
            .iter()
            .filter_map(|e| match e {
                Event::RateLimits(limits) => Some(limits),
                Event::Usage(_) => None,
            })
            .collect();
        assert_eq!(limits.len(), 2);
        let primary = &limits[0].windows[0];
        assert_eq!(
            (
                primary.name.as_str(),
                primary.window_minutes,
                primary.used_percent
            ),
            ("primary", 300, 1.0)
        );
        assert_eq!(
            format_time(primary.resets_at.unwrap()),
            "2026-10-01T14:00:00.000Z"
        );
        assert_eq!(limits[0].windows[1].window_minutes, 10080);
        assert!((limits[1].windows[0].used_percent - 4.0).abs() < f64::EPSILON);
    }

    #[test]
    fn a_count_before_the_session_is_known_is_an_error() {
        let mut log = CodexLog::default();
        let line = r#"{"timestamp":"2026-10-01T10:00:00Z","type":"event_msg","payload":{"type":"token_count","info":null}}"#;
        assert!(matches!(
            log.parse_line(line),
            Err(ParseError::Missing("session_meta"))
        ));
    }

    #[test]
    fn a_restart_is_seen_even_when_the_new_total_passed_the_old_one() {
        let count = |at: &str, total: u64, last: u64| {
            format!(
                r#"{{"timestamp":"{at}","type":"event_msg","payload":{{"type":"token_count","info":{{"total_token_usage":{{"input_tokens":{total},"total_tokens":{total}}},"last_token_usage":{{"input_tokens":{last},"total_tokens":{last}}}}}}}}}"#
            )
        };
        let meta =
            r#"{"timestamp":"2026-10-01T10:00:00Z","type":"session_meta","payload":{"id":"c-2"}}"#;
        let text = [
            meta.to_owned(),
            count("2026-10-01T10:00:01Z", 100, 100),
            // A new process counted 150 from zero: the difference (50) is not its usage.
            count("2026-10-01T10:00:02Z", 150, 150),
        ]
        .join("\n");
        let inputs: Vec<u64> = usage(&events(&text))
            .iter()
            .map(|r| r.tokens.input)
            .collect();
        assert_eq!(inputs, [100, 150]);
    }
}
