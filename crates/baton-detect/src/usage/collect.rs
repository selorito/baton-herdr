//! Finding the agents' logs, following them, and writing NDJSON.
//!
//! Claude Code and Codex logs are JSONL files under a directory each; they are found by
//! scanning, followed with [`Tail`], and read again on file-system events (`notify`) and
//! on a periodic rescan, which also finds directories that appear later. `OpenCode`'s
//! database is polled. Every event goes to stdout as one line, flushed at once;
//! everything else (unreadable files, bad lines) goes to stderr.

use std::collections::{HashMap, HashSet};
use std::io::{self, Write};
use std::path::{Path, PathBuf};
use std::sync::mpsc;
use std::time::{Duration, Instant};

use notify::{RecursiveMode, Watcher};
use time::OffsetDateTime;

use super::claude::ClaudeLog;
use super::codex::CodexLog;
use super::opencode::OpenCodeDb;
use super::{Event, UsageRecord};
use crate::tail::Tail;

/// A session's records are checked against its totals once it has been quiet this long.
const SESSION_QUIET: Duration = Duration::from_secs(60);

/// Where the agents keep their usage.
#[derive(Debug, Clone, Default)]
pub struct Sources {
    pub claude_dir: Option<PathBuf>,
    pub codex_dir: Option<PathBuf>,
    pub opencode_db: Option<PathBuf>,
}

impl Sources {
    /// The agents' default locations, honouring their own environment variables.
    #[must_use]
    pub fn defaults() -> Self {
        let home = std::env::var_os("HOME").map(PathBuf::from);
        let env_dir = |name: &str| std::env::var_os(name).map(PathBuf::from);
        Self {
            claude_dir: env_dir("CLAUDE_CONFIG_DIR")
                .or_else(|| home.as_ref().map(|h| h.join(".claude")))
                .map(|d| d.join("projects")),
            codex_dir: env_dir("CODEX_HOME")
                .or_else(|| home.as_ref().map(|h| h.join(".codex")))
                .map(|d| d.join("sessions")),
            opencode_db: env_dir("XDG_DATA_HOME")
                .or_else(|| home.as_ref().map(|h| h.join(".local/share")))
                .map(|d| d.join("opencode/opencode.db")),
        }
    }
}

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
enum Kind {
    Claude,
    Codex,
}

#[derive(Debug)]
struct Followed {
    kind: Kind,
    tail: Tail,
    codex: CodexLog,
}

#[derive(Debug)]
struct QuietCheck {
    last_record: Instant,
    checked: bool,
}

/// Reads every source and writes what is new.
#[derive(Debug)]
pub struct Collector<W: Write> {
    sources: Sources,
    since: Option<OffsetDateTime>,
    out: W,
    files: HashMap<PathBuf, Followed>,
    claude: ClaudeLog,
    opencode: Option<OpenCodeDb>,
    sessions: HashMap<String, QuietCheck>,
    warned: HashSet<String>,
}

impl<W: Write> Collector<W> {
    /// `since` drops events before it (files not changed since are not even read).
    pub fn new(sources: Sources, since: Option<OffsetDateTime>, out: W) -> Self {
        Self {
            sources,
            since,
            out,
            files: HashMap::new(),
            claude: ClaudeLog::default(),
            opencode: None,
            sessions: HashMap::new(),
            warned: HashSet::new(),
        }
    }

    /// Finds new files and reads everything new in every source.
    ///
    /// # Errors
    /// Only when stdout cannot be written: nobody is listening any more.
    pub fn scan(&mut self) -> io::Result<()> {
        for (kind, dir) in [
            (Kind::Claude, self.sources.claude_dir.clone()),
            (Kind::Codex, self.sources.codex_dir.clone()),
        ] {
            let Some(dir) = dir else { continue };
            let mut found = Vec::new();
            jsonl_files(&dir, kind, &mut found);
            found.sort();
            for path in found {
                if self.files.contains_key(&path) || !self.changed_since(&path) {
                    continue;
                }
                self.files.insert(
                    path.clone(),
                    Followed {
                        kind,
                        tail: Tail::new(path),
                        codex: CodexLog::default(),
                    },
                );
            }
        }
        let mut paths: Vec<PathBuf> = self.files.keys().cloned().collect();
        paths.sort();
        for path in paths {
            self.read_file(&path)?;
        }
        self.poll_opencode(false)
    }

    /// Reads the files named by file-system events, picking up new ones.
    ///
    /// # Errors
    /// Only when stdout cannot be written.
    pub fn read_paths(&mut self, paths: &[PathBuf]) -> io::Result<()> {
        for path in paths {
            if !self.files.contains_key(path) {
                let kind = self.kind_of(path);
                let Some(kind) = kind else { continue };
                self.files.insert(
                    path.clone(),
                    Followed {
                        kind,
                        tail: Tail::new(path.clone()),
                        codex: CodexLog::default(),
                    },
                );
            }
            self.read_file(path)?;
        }
        Ok(())
    }

    /// Checks every `OpenCode` session that has been quiet for a while (or every one,
    /// with `all`), warning when its records and its own totals disagree.
    ///
    /// # Errors
    /// Only when stdout cannot be written.
    pub fn poll_opencode(&mut self, all: bool) -> io::Result<()> {
        if self.opencode.is_none() {
            let Some(path) = self.sources.opencode_db.clone() else {
                return Ok(());
            };
            if !path.exists() {
                self.warn_once(&format!(
                    "{} not found; looking again later",
                    path.display()
                ));
                return Ok(());
            }
            match OpenCodeDb::open(&path) {
                Ok(db) => self.opencode = Some(db),
                Err(error) => {
                    warn(&format!("{}: {error}", path.display()));
                    return Ok(());
                }
            }
        }
        let Some(db) = self.opencode.as_mut() else {
            return Ok(());
        };
        let (records, errors) = match db.poll() {
            Ok(polled) => polled,
            Err(error) => {
                warn(&format!("opencode: {error}"));
                return Ok(());
            }
        };
        for error in errors {
            warn(&format!(
                "opencode message {}: {}",
                error.message_id, error.error
            ));
        }
        let now = Instant::now();
        for record in records {
            self.sessions.insert(
                record.session_id.clone(),
                QuietCheck {
                    last_record: now,
                    checked: false,
                },
            );
            self.emit(&Event::Usage(record))?;
        }
        self.check_quiet_sessions(all);
        Ok(())
    }

    /// Follows the sources until stdout closes: file-system events as they come, the
    /// database every `poll`, a full rescan every `rescan`.
    ///
    /// # Errors
    /// When stdout cannot be written, or no file watcher can be created.
    pub fn follow(&mut self, poll: Duration, rescan: Duration) -> io::Result<()> {
        let (sender, events) = mpsc::channel();
        let mut watcher = notify::recommended_watcher(sender).map_err(io::Error::other)?;
        let mut watching: HashSet<PathBuf> = HashSet::new();
        let mut last_scan = Instant::now();
        // Watch first, then scan: a line written in between is seen by one of them.
        self.watch_new_dirs(&mut watcher, &mut watching);
        self.scan()?;
        loop {
            let mut paths = Vec::new();
            if let Ok(event) = events.recv_timeout(poll) {
                collect_paths(event, &mut paths);
                while let Ok(event) = events.try_recv() {
                    collect_paths(event, &mut paths);
                }
            }
            paths.sort();
            paths.dedup();
            self.read_paths(&paths)?;
            if last_scan.elapsed() >= rescan {
                self.watch_new_dirs(&mut watcher, &mut watching);
                self.scan()?;
                last_scan = Instant::now();
            } else {
                self.poll_opencode(false)?;
            }
        }
    }

    /// Watches the log directories that exist now and are not watched yet.
    fn watch_new_dirs(&self, watcher: &mut impl Watcher, watching: &mut HashSet<PathBuf>) {
        for dir in [&self.sources.claude_dir, &self.sources.codex_dir]
            .into_iter()
            .flatten()
        {
            if !watching.contains(dir) && dir.is_dir() {
                match watcher.watch(dir, RecursiveMode::Recursive) {
                    Ok(()) => {
                        watching.insert(dir.clone());
                    }
                    Err(error) => warn(&format!("watching {}: {error}", dir.display())),
                }
            }
        }
    }

    fn read_file(&mut self, path: &Path) -> io::Result<()> {
        let Some(followed) = self.files.get_mut(path) else {
            return Ok(());
        };
        let lines = match followed.tail.read_lines() {
            Ok(lines) => lines,
            Err(error) => {
                if error.kind() == io::ErrorKind::NotFound {
                    self.files.remove(path);
                } else {
                    warn(&format!("{}: {error}", path.display()));
                }
                return Ok(());
            }
        };
        if lines.restarted {
            followed.codex = CodexLog::default();
        }
        let kind = followed.kind;
        let mut events = Vec::new();
        for line in &lines.lines {
            let parsed = match kind {
                Kind::Claude => self
                    .claude
                    .parse_line(line)
                    .map(|record| record.map(Event::Usage).into_iter().collect()),
                Kind::Codex => self
                    .files
                    .get_mut(path)
                    .map_or(Ok(Vec::new()), |f| f.codex.parse_line(line)),
            };
            match parsed {
                Ok(parsed) => events.extend(parsed),
                Err(error) => warn(&format!("{}: {error}", path.display())),
            }
        }
        for event in events {
            self.emit(&event)?;
        }
        Ok(())
    }

    fn emit(&mut self, event: &Event) -> io::Result<()> {
        let at = match event {
            Event::Usage(UsageRecord { at, .. }) => *at,
            Event::RateLimits(limits) => limits.at,
        };
        if self.since.is_some_and(|since| at < since) {
            return Ok(());
        }
        serde_json::to_writer(&mut self.out, event).map_err(io::Error::other)?;
        self.out.write_all(b"\n")?;
        self.out.flush()
    }

    fn check_quiet_sessions(&mut self, all: bool) {
        let Some(db) = self.opencode.as_ref() else {
            return;
        };
        for (session, check) in &mut self.sessions {
            if check.checked || !(all || check.last_record.elapsed() >= SESSION_QUIET) {
                continue;
            }
            check.checked = true;
            match db.check_session(session) {
                Ok(Some(difference)) => warn(&format!("opencode {difference}")),
                Ok(None) => {}
                Err(error) => warn(&format!("opencode session {session}: {error}")),
            }
        }
    }

    fn changed_since(&self, path: &Path) -> bool {
        let Some(since) = self.since else {
            return true;
        };
        let modified = std::fs::metadata(path).and_then(|m| m.modified());
        modified.map_or(true, |m| OffsetDateTime::from(m) >= since)
    }

    fn kind_of(&self, path: &Path) -> Option<Kind> {
        let under = |dir: &Option<PathBuf>| dir.as_ref().is_some_and(|d| path.starts_with(d));
        if under(&self.sources.claude_dir) && is_log(path, Kind::Claude) {
            Some(Kind::Claude)
        } else if under(&self.sources.codex_dir) && is_log(path, Kind::Codex) {
            Some(Kind::Codex)
        } else {
            None
        }
    }

    fn warn_once(&mut self, message: &str) {
        if self.warned.insert(message.to_owned()) {
            warn(message);
        }
    }

    /// Writes nothing more; used by `--once` after the first scan.
    ///
    /// # Errors
    /// Only when stdout cannot be written.
    pub fn finish(&mut self) -> io::Result<()> {
        self.check_quiet_sessions(true);
        self.out.flush()
    }
}

fn is_log(path: &Path, kind: Kind) -> bool {
    let name = path.file_name().and_then(|n| n.to_str()).unwrap_or("");
    let jsonl = Path::new(name)
        .extension()
        .is_some_and(|e| e.eq_ignore_ascii_case("jsonl"));
    jsonl && (kind == Kind::Claude || name.starts_with("rollout-"))
}

fn jsonl_files(dir: &Path, kind: Kind, found: &mut Vec<PathBuf>) {
    let Ok(entries) = std::fs::read_dir(dir) else {
        return;
    };
    for entry in entries.flatten() {
        let path = entry.path();
        match entry.file_type() {
            Ok(t) if t.is_dir() => jsonl_files(&path, kind, found),
            Ok(t) if t.is_file() && is_log(&path, kind) => found.push(path),
            _ => {}
        }
    }
}

fn collect_paths(event: notify::Result<notify::Event>, paths: &mut Vec<PathBuf>) {
    if let Ok(event) = event {
        paths.extend(event.paths);
    }
}

fn warn(message: &str) {
    eprintln!("baton-detect: {message}");
}
