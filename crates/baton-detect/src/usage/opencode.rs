//! `OpenCode`'s database: `~/.local/share/opencode/opencode.db` (`SQLite`, WAL).
//!
//! Each assistant message's `data` holds its own tokens and, once the response is
//! done, `time.completed`. Messages are read in `time_updated` order from a cursor and
//! reported once, when completed. `OpenCode` counts reasoning apart from output; here
//! it is part of `output`, as for the other agents.
//!
//! The `session` table keeps running totals per session. They are not used for the
//! records (they have no times), only to check them: [`OpenCodeDb::check_session`].

use std::collections::HashSet;
use std::path::Path;

use rusqlite::{Connection, OpenFlags, params};
use serde::Deserialize;

use super::{Agent, ParseError, Tokens, UsageRecord, from_unix_millis};

#[derive(Debug, Deserialize)]
struct Data {
    role: Option<String>,
    #[serde(rename = "modelID")]
    model_id: Option<String>,
    time: Option<Times>,
    tokens: Option<MessageTokens>,
}

#[derive(Debug, Deserialize)]
struct Times {
    completed: Option<i64>,
}

#[derive(Debug, Default, Deserialize)]
struct MessageTokens {
    #[serde(default)]
    input: u64,
    #[serde(default)]
    output: u64,
    #[serde(default)]
    reasoning: u64,
    #[serde(default)]
    cache: Cache,
}

#[derive(Debug, Default, Deserialize)]
struct Cache {
    #[serde(default)]
    read: u64,
    #[serde(default)]
    write: u64,
}

/// Why the database could not be read.
#[derive(Debug, thiserror::Error)]
pub enum DbError {
    #[error("sqlite: {0}")]
    Sqlite(#[from] rusqlite::Error),
}

/// A row that could not be turned into a record.
#[derive(Debug)]
pub struct RowError {
    pub message_id: String,
    pub error: ParseError,
}

/// Reads completed assistant messages, each once.
#[derive(Debug)]
pub struct OpenCodeDb {
    conn: Connection,
    cursor: i64,
    reported: HashSet<String>,
}

impl OpenCodeDb {
    /// Opens the database read-only; `OpenCode` keeps writing to it.
    ///
    /// # Errors
    /// The file cannot be opened.
    pub fn open(path: &Path) -> Result<Self, DbError> {
        let flags = OpenFlags::SQLITE_OPEN_READ_ONLY | OpenFlags::SQLITE_OPEN_NO_MUTEX;
        Ok(Self::from_connection(Connection::open_with_flags(
            path, flags,
        )?))
    }

    #[must_use]
    pub fn from_connection(conn: Connection) -> Self {
        Self {
            conn,
            cursor: 0,
            reported: HashSet::new(),
        }
    }

    /// Messages completed since the last call.
    ///
    /// # Errors
    /// The query fails (for example while the schema is being migrated); rows that do
    /// not parse are returned as [`RowError`]s instead.
    pub fn poll(&mut self) -> Result<(Vec<UsageRecord>, Vec<RowError>), DbError> {
        let mut statement = self.conn.prepare(
            "SELECT id, session_id, time_updated, data FROM message \
             WHERE time_updated >= ?1 ORDER BY time_updated, id",
        )?;
        let rows = statement.query_map(params![self.cursor], |row| {
            Ok((
                row.get::<_, String>(0)?,
                row.get::<_, String>(1)?,
                row.get::<_, i64>(2)?,
                row.get::<_, String>(3)?,
            ))
        })?;
        let mut records = Vec::new();
        let mut errors = Vec::new();
        let mut cursor = self.cursor;
        for row in rows {
            let (id, session, updated, data) = row?;
            cursor = cursor.max(updated);
            if self.reported.contains(&id) {
                continue;
            }
            match record(&id, &session, &data) {
                Ok(Some(record)) => {
                    self.reported.insert(id);
                    records.push(record);
                }
                Ok(None) => {}
                Err(error) => {
                    self.reported.insert(id.clone()); // report a bad row once
                    errors.push(RowError {
                        message_id: id,
                        error,
                    });
                }
            }
        }
        // Rows updated in the same millisecond as the cursor are read again next time;
        // `reported` keeps them from being counted twice.
        self.cursor = cursor;
        Ok((records, errors))
    }

    /// Compares the records of `session` with the session's own running totals.
    ///
    /// Returns a description of the difference, or `None` when they agree.
    ///
    /// # Errors
    /// The query fails.
    pub fn check_session(&self, session: &str) -> Result<Option<String>, DbError> {
        let totals = self.conn.query_row(
            "SELECT tokens_input, tokens_output, tokens_reasoning, tokens_cache_read, \
             tokens_cache_write FROM session WHERE id = ?1",
            params![session],
            |row| {
                let count = |i| row.get::<_, i64>(i).map(|n| u64::try_from(n).unwrap_or(0));
                Ok((count(0)?, count(1)?, count(2)?, count(3)?, count(4)?))
            },
        )?;
        let mut statement = self
            .conn
            .prepare("SELECT id, data FROM message WHERE session_id = ?1")?;
        let mut sum = (0, 0, 0, 0, 0);
        for row in statement.query_map(params![session], |row| {
            Ok((row.get::<_, String>(0)?, row.get::<_, String>(1)?))
        })? {
            let (id, data) = row?;
            if let Ok(Some(record)) = record(&id, session, &data) {
                let reasoning = record.tokens.reasoning.unwrap_or(0);
                sum.0 += record.tokens.input;
                sum.1 += record.tokens.output - reasoning;
                sum.2 += reasoning;
                sum.3 += record.tokens.cache_read;
                sum.4 += record.tokens.cache_write;
            }
        }
        Ok((sum != totals).then(|| {
            format!(
                "session {session}: messages sum to (input, output, reasoning, cache read, \
                 cache write) {sum:?}, the session row says {totals:?}"
            )
        }))
    }
}

fn record(id: &str, session: &str, data: &str) -> Result<Option<UsageRecord>, ParseError> {
    let data: Data = serde_json::from_str(data)?;
    if data.role.as_deref() != Some("assistant") {
        return Ok(None);
    }
    let Some(completed) = data.time.and_then(|t| t.completed) else {
        return Ok(None); // still streaming
    };
    let tokens = data.tokens.ok_or(ParseError::Missing("tokens"))?;
    let at = from_unix_millis(completed).ok_or(ParseError::Missing("time.completed"))?;
    Ok(Some(UsageRecord {
        agent: Agent::Opencode,
        session_id: session.to_owned(),
        record_id: format!("opencode:{id}"),
        at,
        model: data.model_id,
        tokens: Tokens {
            input: tokens.input,
            output: tokens.output + tokens.reasoning,
            cache_read: tokens.cache.read,
            cache_write: tokens.cache.write,
            reasoning: Some(tokens.reasoning),
        },
    }))
}

/// The two tables, as `OpenCode` 1.18 creates them (the columns read here).
pub const TEST_SCHEMA: &str = "
    CREATE TABLE session (
        id TEXT PRIMARY KEY,
        tokens_input INTEGER NOT NULL DEFAULT 0, tokens_output INTEGER NOT NULL DEFAULT 0,
        tokens_reasoning INTEGER NOT NULL DEFAULT 0, tokens_cache_read INTEGER NOT NULL DEFAULT 0,
        tokens_cache_write INTEGER NOT NULL DEFAULT 0);
    CREATE TABLE message (
        id TEXT PRIMARY KEY, session_id TEXT NOT NULL,
        time_created INTEGER NOT NULL, time_updated INTEGER NOT NULL, data TEXT NOT NULL);
";

#[cfg(test)]
mod tests {
    use super::*;
    use crate::usage::format_time;

    fn db() -> OpenCodeDb {
        let conn = Connection::open_in_memory().unwrap();
        conn.execute_batch(TEST_SCHEMA).unwrap();
        OpenCodeDb::from_connection(conn)
    }

    fn message(db: &OpenCodeDb, id: &str, updated: i64, data: &str) {
        db.conn
            .execute(
                "INSERT OR REPLACE INTO message VALUES (?1, 's-1', ?2, ?2, ?3)",
                params![id, updated, data],
            )
            .unwrap();
    }

    const DONE: &str = r#"{"role":"assistant","modelID":"big-pickle","time":{"created":1790755334884,"completed":1790755345154},"tokens":{"input":423,"output":689,"reasoning":296,"cache":{"read":10398,"write":0}}}"#;
    const STREAMING: &str = r#"{"role":"assistant","modelID":"big-pickle","time":{"created":1790755400000},"tokens":{"input":5,"output":1,"reasoning":0,"cache":{"read":0,"write":0}}}"#;

    #[test]
    fn a_message_is_reported_once_when_it_completes() {
        let mut db = db();
        message(&db, "m1", 100, DONE);
        message(&db, "m2", 100, STREAMING);
        message(&db, "u1", 90, r#"{"role":"user","time":{"created":1}}"#);

        let (records, errors) = db.poll().unwrap();
        assert!(errors.is_empty());
        assert_eq!(records.len(), 1);
        let first = &records[0];
        assert_eq!(first.record_id, "opencode:m1");
        assert_eq!(format_time(first.at), "2026-09-30T08:02:25.154Z");
        assert_eq!(
            first.tokens,
            Tokens {
                input: 423,
                output: 985,
                cache_read: 10398,
                cache_write: 0,
                reasoning: Some(296)
            }
        );
        // m2 completes later; m1 in the same millisecond is not reported again.
        message(
            &db,
            "m2",
            200,
            &STREAMING.replace(
                r#""created":1790755400000"#,
                r#""created":1790755400000,"completed":1790755401000"#,
            ),
        );
        let (records, _) = db.poll().unwrap();
        let ids: Vec<&str> = records.iter().map(|r| r.record_id.as_str()).collect();
        assert_eq!(ids, ["opencode:m2"]);
        assert!(db.poll().unwrap().0.is_empty());
    }

    #[test]
    fn a_broken_row_is_reported_and_skipped() {
        let mut db = db();
        message(&db, "bad", 100, "{not json");
        message(&db, "m1", 101, DONE);
        let (records, errors) = db.poll().unwrap();
        assert_eq!(records.len(), 1);
        assert_eq!(errors.len(), 1);
        assert_eq!(errors[0].message_id, "bad");
    }

    #[test]
    fn session_totals_check_the_records() {
        let db = db();
        message(&db, "m1", 100, DONE);
        db.conn
            .execute(
                "INSERT INTO session VALUES ('s-1', 423, 689, 296, 10398, 0)",
                [],
            )
            .unwrap();
        assert_eq!(db.check_session("s-1").unwrap(), None);
        db.conn
            .execute("UPDATE session SET tokens_output = 700", [])
            .unwrap();
        assert!(db.check_session("s-1").unwrap().unwrap().contains("689"));
    }
}
