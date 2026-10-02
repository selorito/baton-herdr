//! Claude Code transcripts: `~/.claude/projects/<project>/<session-id>.jsonl`.
//!
//! Every assistant record carries the usage of the API response it belongs to. One
//! response is split into several records (one per content block), each repeating the
//! same `message.id` and usage, so records are counted once per `message.id`. The first
//! record of a response is the one reported.
//!
//! Message ids are the API's own and unique everywhere, and a resumed session may
//! repeat earlier messages in a new file, so one [`ClaudeLog`] reads all transcripts and
//! the id alone identifies a response.

use std::collections::HashSet;

use serde::Deserialize;

use super::{Agent, ParseError, Tokens, UsageRecord, parse_json, parse_time};

/// Model name Claude Code writes for responses it made up itself (errors, interrupts).
const SYNTHETIC_MODEL: &str = "<synthetic>";

#[derive(Debug, Deserialize)]
struct Line {
    #[serde(rename = "type")]
    kind: Option<String>,
    #[serde(rename = "sessionId")]
    session_id: Option<String>,
    timestamp: Option<String>,
    message: Option<Message>,
}

#[derive(Debug, Deserialize)]
struct Message {
    id: Option<String>,
    model: Option<String>,
    usage: Option<Usage>,
}

// Field names are Claude Code's.
#[allow(clippy::struct_field_names)]
#[derive(Debug, Deserialize)]
struct Usage {
    #[serde(default)]
    input_tokens: u64,
    #[serde(default)]
    output_tokens: u64,
    #[serde(default)]
    cache_creation_input_tokens: u64,
    #[serde(default)]
    cache_read_input_tokens: u64,
}

/// Reads transcripts line by line, remembering which responses it has reported.
#[derive(Debug, Default)]
pub struct ClaudeLog {
    seen: HashSet<String>,
}

impl ClaudeLog {
    /// The usage record in `line`, if it starts a response not seen before.
    ///
    /// # Errors
    /// A line that is not JSON, or an assistant usage record without a session, message
    /// id or valid timestamp. The caller reports it and goes on.
    pub fn parse_line(&mut self, line: &str) -> Result<Option<UsageRecord>, ParseError> {
        let line: Line = parse_json(line)?;
        if line.kind.as_deref() != Some("assistant") {
            return Ok(None);
        }
        let Some(message) = line.message else {
            return Ok(None);
        };
        let Some(usage) = message.usage else {
            return Ok(None);
        };
        if message.model.as_deref() == Some(SYNTHETIC_MODEL) {
            return Ok(None);
        }
        let session = line.session_id.ok_or(ParseError::Missing("sessionId"))?;
        let id = message.id.ok_or(ParseError::Missing("message.id"))?;
        let at = parse_time(&line.timestamp.ok_or(ParseError::Missing("timestamp"))?)?;
        if self.seen.contains(&id) {
            return Ok(None);
        }
        let record_id = format!("claude:{id}");
        self.seen.insert(id);
        Ok(Some(UsageRecord {
            agent: Agent::Claude,
            record_id,
            session_id: session,
            at,
            model: message.model,
            tokens: Tokens {
                input: usage.input_tokens,
                output: usage.output_tokens,
                cache_read: usage.cache_read_input_tokens,
                cache_write: usage.cache_creation_input_tokens,
                reasoning: None,
            },
        }))
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::usage::format_time;

    const SPLIT: &str = include_str!("../../tests/data/claude-split.jsonl");

    fn records(text: &str) -> (Vec<UsageRecord>, usize) {
        let mut log = ClaudeLog::default();
        let mut errors = 0;
        let mut out = Vec::new();
        for line in text.lines().filter(|l| !l.trim().is_empty()) {
            match log.parse_line(line) {
                Ok(Some(record)) => out.push(record),
                Ok(None) => {}
                Err(_) => errors += 1,
            }
        }
        (out, errors)
    }

    #[test]
    fn a_response_split_over_records_counts_once() {
        let (records, errors) = records(SPLIT);
        let ids: Vec<&str> = records.iter().map(|r| r.record_id.as_str()).collect();
        assert_eq!(
            ids,
            [
                "claude:msg_aaa",
                "claude:msg_bbb",
                "claude:msg_ccc" // a sidechain (subagent) response counts too
            ]
        );
        // One broken line; the synthetic and the user records are not usage.
        assert_eq!(errors, 1);
        let first = &records[0];
        assert_eq!(format_time(first.at), "2026-10-01T10:00:01.000Z");
        assert_eq!(first.model.as_deref(), Some("claude-opus-5-5"));
        assert_eq!(
            first.tokens,
            Tokens {
                input: 3,
                output: 120,
                cache_read: 20000,
                cache_write: 500,
                reasoning: None
            }
        );
    }

    #[test]
    fn a_response_repeated_in_a_resumed_sessions_file_counts_once() {
        let mut log = ClaudeLog::default();
        let line = |session: &str| {
            format!(
                r#"{{"type":"assistant","sessionId":"{session}","timestamp":"2026-10-01T10:00:00Z","message":{{"id":"msg_x","model":"m","usage":{{"input_tokens":1,"output_tokens":1}}}}}}"#
            )
        };
        assert!(log.parse_line(&line("a")).unwrap().is_some());
        assert!(log.parse_line(&line("a")).unwrap().is_none());
        assert!(log.parse_line(&line("b")).unwrap().is_none());
    }

    #[test]
    fn a_usage_record_without_an_id_is_an_error_not_a_guess() {
        let mut log = ClaudeLog::default();
        let line = r#"{"type":"assistant","sessionId":"a","timestamp":"2026-10-01T10:00:00Z","message":{"model":"m","usage":{"input_tokens":1}}}"#;
        assert!(matches!(
            log.parse_line(line),
            Err(ParseError::Missing("message.id"))
        ));
    }
}
