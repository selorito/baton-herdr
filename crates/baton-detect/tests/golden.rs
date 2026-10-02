//! Golden tests: the captured usage samples in `fixtures/` against `tests/golden/`.
//!
//! The expected files are also what the Python contract test compares the binary's
//! output with (`tests/integration/test_usage_contract_binary.py`). Regenerate them
//! deliberately with `UPDATE_GOLDEN=1 cargo test --test golden`.
//!
//! The samples were captured before record ids were pseudonymised: every Claude
//! `message.id` reads `<redacted>`. Deduplication therefore cannot be checked on the
//! Claude sample (its four records, two responses split in two, collapse into one);
//! `tests/data/claude-split.jsonl` covers it instead.
#![allow(clippy::unwrap_used)] // test helpers: a failure should stop the test, with the error

use std::path::PathBuf;

use baton_detect::usage::Event;
use baton_detect::usage::claude::ClaudeLog;
use baton_detect::usage::codex::CodexLog;
use baton_detect::usage::opencode::{OpenCodeDb, TEST_SCHEMA};
use rusqlite::{Connection, params};
use serde_json::Value;

fn repo() -> PathBuf {
    PathBuf::from(env!("CARGO_MANIFEST_DIR")).join("../..")
}

fn sample(agent: &str) -> String {
    std::fs::read_to_string(repo().join(format!("fixtures/{agent}/usage-sample.jsonl"))).unwrap()
}

fn check(agent: &str, events: &[Event]) {
    let actual: String = events
        .iter()
        .map(|e| serde_json::to_string(e).unwrap() + "\n")
        .collect();
    let path = repo().join(format!("crates/baton-detect/tests/golden/{agent}.ndjson"));
    if std::env::var_os("UPDATE_GOLDEN").is_some() {
        std::fs::write(&path, &actual).unwrap();
    }
    let expected = std::fs::read_to_string(&path).unwrap();
    let parse = |text: &str| -> Vec<Value> {
        text.lines()
            .map(|l| serde_json::from_str(l).unwrap())
            .collect()
    };
    assert_eq!(
        parse(&actual),
        parse(&expected),
        "{agent}: differs from {}",
        path.display()
    );
}

#[test]
fn claude_sample() {
    let mut log = ClaudeLog::default();
    let events: Vec<Event> = sample("claude")
        .lines()
        .filter_map(|line| log.parse_line(line).unwrap())
        .map(Event::Usage)
        .collect();
    // Two responses in the sample, one after redaction (see the module comment).
    assert_eq!(events.len(), 1);
    check("claude", &events);
}

#[test]
fn codex_sample() {
    let mut log = CodexLog::default();
    let events: Vec<Event> = sample("codex")
        .lines()
        .flat_map(|line| log.parse_line(line).unwrap())
        .collect();
    check("codex", &events);
}

/// The sample holds the session row and the messages' `data`; ids and the session id
/// were redacted, so messages get `m1`, `m2`, … in sample order.
fn opencode_db(sample: &str) -> Connection {
    let conn = Connection::open_in_memory().unwrap();
    conn.execute_batch(TEST_SCHEMA).unwrap();
    let mut number = 0;
    for line in sample.lines() {
        let record: Value = serde_json::from_str(line).unwrap();
        match record["type"].as_str() {
            Some("sqlite:session") => {
                let row = &record["row"];
                conn.execute(
                    "INSERT INTO session VALUES (?1, ?2, ?3, ?4, ?5, ?6)",
                    params![
                        row["id"].as_str().unwrap(),
                        row["tokens_input"].as_i64(),
                        row["tokens_output"].as_i64(),
                        row["tokens_reasoning"].as_i64(),
                        row["tokens_cache_read"].as_i64(),
                        row["tokens_cache_write"].as_i64(),
                    ],
                )
                .unwrap();
            }
            Some("sqlite:message") => {
                number += 1;
                let data = &record["data"];
                let time = &data["time"];
                let updated = time["completed"].as_i64().or(time["created"].as_i64());
                conn.execute(
                    "INSERT INTO message VALUES (?1, '<redacted>', ?2, ?2, ?3)",
                    params![format!("m{number}"), updated, data.to_string()],
                )
                .unwrap();
            }
            _ => {}
        }
    }
    conn
}

#[test]
fn opencode_sample() {
    let mut db = OpenCodeDb::from_connection(opencode_db(&sample("opencode")));
    let (records, errors) = db.poll().unwrap();
    assert!(errors.is_empty());
    let events: Vec<Event> = records.into_iter().map(Event::Usage).collect();
    check("opencode", &events);
    // The sample keeps a few of the session's messages, so its totals do not add up.
    assert!(db.check_session("<redacted>").unwrap().is_some());
}
