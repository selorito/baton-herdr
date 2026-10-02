//! Usage records and rate-limit observations: the `usage-event.v1` contract.
//!
//! The types here serialize to exactly the JSON that `schemas/usage-event.v1.json`
//! describes (generated from `baton_herdr.core.usage`). Each agent's parser turns its
//! own log format into them.

pub mod claude;
pub mod codex;
pub mod opencode;

use serde::{Serialize, Serializer};
use time::OffsetDateTime;
use time::format_description::well_known::Rfc3339;
use time::macros::format_description;

/// The agents whose usage is collected.
#[derive(Debug, Clone, Copy, PartialEq, Eq, Hash, Serialize)]
#[serde(rename_all = "lowercase")]
pub enum Agent {
    Claude,
    Codex,
    Opencode,
}

impl Agent {
    #[must_use]
    pub fn name(self) -> &'static str {
        match self {
            Agent::Claude => "claude",
            Agent::Codex => "codex",
            Agent::Opencode => "opencode",
        }
    }
}

/// Token counts in the shared vocabulary.
///
/// `input` excludes cache reads; `output` includes reasoning; `reasoning` is the part of
/// `output` spent on reasoning, when the agent reports it.
#[derive(Debug, Clone, Copy, PartialEq, Eq, Default, Serialize)]
pub struct Tokens {
    pub input: u64,
    pub output: u64,
    pub cache_read: u64,
    pub cache_write: u64,
    pub reasoning: Option<u64>,
}

/// Tokens one agent response used.
#[derive(Debug, Clone, PartialEq, Eq, Serialize)]
pub struct UsageRecord {
    pub agent: Agent,
    pub session_id: String,
    /// Stable across re-reads, so batond can store each response once.
    pub record_id: String,
    #[serde(serialize_with = "utc_millis")]
    pub at: OffsetDateTime,
    pub model: Option<String>,
    pub tokens: Tokens,
}

/// One usage window an agent reports about itself.
#[derive(Debug, Clone, PartialEq, Serialize)]
pub struct RateLimitWindow {
    pub name: String,
    pub window_minutes: u64,
    pub used_percent: f64,
    #[serde(serialize_with = "optional_utc_millis")]
    pub resets_at: Option<OffsetDateTime>,
}

/// An agent's report of its usage windows.
#[derive(Debug, Clone, PartialEq, Serialize)]
pub struct RateLimitObservation {
    pub agent: Agent,
    pub session_id: String,
    #[serde(serialize_with = "utc_millis")]
    pub at: OffsetDateTime,
    pub windows: Vec<RateLimitWindow>,
}

/// One NDJSON line.
#[derive(Debug, Clone, PartialEq, Serialize)]
#[serde(tag = "kind")]
pub enum Event {
    #[serde(rename = "usage")]
    Usage(UsageRecord),
    #[serde(rename = "rate_limits")]
    RateLimits(RateLimitObservation),
}

/// Why a log line yielded nothing; reported on stderr, never fatal.
#[derive(Debug, thiserror::Error)]
pub enum ParseError {
    #[error("not JSON: {0}")]
    Json(#[from] serde_json::Error),
    #[error("bad timestamp {0:?}")]
    Timestamp(String),
    #[error("usage record without {0}")]
    Missing(&'static str),
}

/// Parses the RFC 3339 times agents write (`2026-09-30T07:15:26.612Z`).
///
/// # Errors
/// [`ParseError::Timestamp`] when `text` is not RFC 3339.
pub fn parse_time(text: &str) -> Result<OffsetDateTime, ParseError> {
    OffsetDateTime::parse(text, &Rfc3339).map_err(|_| ParseError::Timestamp(text.to_owned()))
}

/// A Unix time in seconds, as Codex writes `resets_at`.
#[must_use]
pub fn from_unix(seconds: i64) -> Option<OffsetDateTime> {
    OffsetDateTime::from_unix_timestamp(seconds).ok()
}

/// A Unix time in milliseconds, as `OpenCode` writes it.
#[must_use]
pub fn from_unix_millis(millis: i64) -> Option<OffsetDateTime> {
    OffsetDateTime::from_unix_timestamp_nanos(i128::from(millis) * 1_000_000).ok()
}

/// `2026-09-30T07:15:26.612Z`: UTC, always three fractional digits.
#[must_use]
pub fn format_time(at: OffsetDateTime) -> String {
    let format =
        format_description!("[year]-[month]-[day]T[hour]:[minute]:[second].[subsecond digits:3]Z");
    at.to_offset(time::UtcOffset::UTC)
        .format(&format)
        .unwrap_or_else(|_| at.unix_timestamp().to_string())
}

fn utc_millis<S: Serializer>(at: &OffsetDateTime, serializer: S) -> Result<S::Ok, S::Error> {
    serializer.serialize_str(&format_time(*at))
}

#[allow(clippy::ref_option)] // serde's serialize_with hands over &Option<T>
fn optional_utc_millis<S: Serializer>(
    at: &Option<OffsetDateTime>,
    serializer: S,
) -> Result<S::Ok, S::Error> {
    match at {
        Some(at) => utc_millis(at, serializer),
        None => serializer.serialize_none(),
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn times_are_written_in_utc_with_milliseconds() {
        let at = parse_time("2026-09-30T10:15:26.6129+03:00").unwrap();
        assert_eq!(format_time(at), "2026-09-30T07:15:26.612Z");
        assert_eq!(
            format_time(from_unix(1_790_770_250).unwrap()),
            "2026-09-30T12:10:50.000Z"
        );
        assert_eq!(
            format_time(from_unix_millis(1_790_755_334_879).unwrap()),
            "2026-09-30T08:02:14.879Z"
        );
    }

    #[test]
    fn an_event_serializes_with_its_kind() {
        let event = Event::Usage(UsageRecord {
            agent: Agent::Claude,
            session_id: "s".into(),
            record_id: "claude:s:m".into(),
            at: from_unix(0).unwrap(),
            model: None,
            tokens: Tokens::default(),
        });
        let json = serde_json::to_value(&event).unwrap();
        assert_eq!(json["kind"], "usage");
        assert_eq!(json["agent"], "claude");
        assert_eq!(json["at"], "1970-01-01T00:00:00.000Z");
        assert_eq!(json["tokens"]["reasoning"], serde_json::Value::Null);
    }
}
