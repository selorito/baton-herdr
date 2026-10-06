//! `baton-detect`: reports what coding agents leave behind to batond as NDJSON
//! (ADR 0011). Phase 1: `baton-detect usage`; phase 2: `baton-detect classify`. And
//! `baton-detect statusline`, Claude Code's status line command in baton's sessions,
//! which keeps Claude's rate limits for `usage` (ADR 0015).

use std::fs::{self, OpenOptions};
use std::io::{self, BufRead, BufWriter, Read, Seek, SeekFrom, Write};
use std::path::{Path, PathBuf};
use std::process::ExitCode;
use std::time::Duration;

use baton_detect::classify::rules::RuleBook;
use baton_detect::classify::{Request, classify, classify_using};
use baton_detect::usage::claude_status;
use baton_detect::usage::collect::{Collector, Sources};
use baton_detect::usage::parse_time;
use clap::{Parser, Subcommand};

#[derive(Debug, Parser)]
#[command(name = "baton-detect", version, about)]
struct Cli {
    #[command(subcommand)]
    command: Option<Command>,
}

#[derive(Debug, Subcommand)]
enum Command {
    /// Write the agents' token usage and rate limits to stdout, one JSON object per line
    /// (schemas/usage-event.v1.json). Follows the logs until stdout closes.
    Usage(UsageArgs),
    /// Classify screens: one detection request per stdin line, one result per stdout line
    /// (schemas/detector-*.v1.json). Each result is flushed as soon as it is written.
    Classify(ClassifyArgs),
    /// Claude Code's status line command: reads its JSON on stdin, appends its rate limits
    /// to LOG when they changed, and prints them (`5h 23% · 7d 41%`). Never fails, so a
    /// problem here never shows in Claude's footer.
    Statusline(StatuslineArgs),
}

#[derive(Debug, clap::Args)]
struct StatuslineArgs {
    /// The log the windows are appended to; `baton-detect usage --claude-status` reads it.
    #[arg(long, value_name = "LOG")]
    log: PathBuf,
}

#[derive(Debug, clap::Args)]
struct ClassifyArgs {
    /// Read the rules from DIR/<agent>.toml instead of the built-in ones, to try a change
    /// to them without a rebuild.
    #[arg(long, value_name = "DIR")]
    rules: Option<PathBuf>,
}

#[derive(Debug, clap::Args)]
struct UsageArgs {
    /// Read what is there, then exit instead of following.
    #[arg(long)]
    once: bool,
    /// Leave out events before this RFC 3339 time (a starting point, not exact:
    /// the receiver drops repeats by record id).
    #[arg(long)]
    since: Option<String>,
    #[arg(
        long,
        help = "Claude Code transcripts [default: $CLAUDE_CONFIG_DIR/projects or ~/.claude/projects]"
    )]
    claude_dir: Option<PathBuf>,
    #[arg(
        long,
        help = "Codex rollouts [default: $CODEX_HOME/sessions or ~/.codex/sessions]"
    )]
    codex_dir: Option<PathBuf>,
    #[arg(
        long,
        help = "OpenCode's database [default: $XDG_DATA_HOME/opencode/opencode.db, \
                else ~/.local/share/opencode/opencode.db]"
    )]
    opencode_db: Option<PathBuf>,
    /// Claude Code's rate limits, as `baton-detect statusline --log` writes them.
    #[arg(long, value_name = "LOG")]
    claude_status: Option<PathBuf>,
    /// How often the database is polled and file events are handled, in milliseconds.
    #[arg(long, default_value_t = 2000)]
    poll_ms: u64,
    /// How often directories are scanned again for new files, in seconds.
    #[arg(long, default_value_t = 30)]
    rescan_secs: u64,
}

fn main() -> ExitCode {
    let cli = Cli::parse();
    match cli.command {
        None => ExitCode::SUCCESS,
        Some(Command::Usage(args)) => usage(args),
        Some(Command::Statusline(args)) => statusline(&args),
        Some(Command::Classify(args)) => {
            let book = match args.rules.as_deref().map(RuleBook::load).transpose() {
                Ok(book) => book,
                Err(error) => {
                    eprintln!("baton-detect: --rules: {error}");
                    return ExitCode::from(2);
                }
            };
            classified(classify_lines(
                io::stdin().lock(),
                io::stdout().lock(),
                book.as_ref(),
            ))
        }
    }
}

fn classified(result: io::Result<()>) -> ExitCode {
    match result {
        Ok(()) => ExitCode::SUCCESS,
        Err(error) if error.kind() == io::ErrorKind::BrokenPipe => ExitCode::SUCCESS,
        Err(error) => {
            eprintln!("baton-detect: {error}");
            ExitCode::from(2)
        }
    }
}

/// The detector's NDJSON contract, the same as `baton detect`: a request that cannot be
/// read, or cannot be classified (an unknown time zone), stops with exit status 2.
fn classify_lines(
    input: impl BufRead,
    mut output: impl Write,
    book: Option<&RuleBook>,
) -> io::Result<()> {
    for (number, line) in input.lines().enumerate() {
        let line = line?;
        if line.trim().is_empty() {
            continue;
        }
        let invalid = |what: String| {
            io::Error::new(
                io::ErrorKind::InvalidData,
                format!("line {}: {what}", number + 1),
            )
        };
        let request: Request = serde_json::from_str(&line)
            .map_err(|e| invalid(format!("invalid detection request: {e}")))?;
        let result = match book {
            Some(book) => classify_using(&request, book.for_agent(request.agent)),
            None => classify(&request),
        }
        .map_err(|e| invalid(e.to_string()))?;
        serde_json::to_writer(&mut output, &result)?;
        output.write_all(b"\n")?;
        output.flush()?;
    }
    Ok(())
}

fn usage(args: UsageArgs) -> ExitCode {
    let since = match args.since.as_deref().map(parse_time).transpose() {
        Ok(since) => since,
        Err(error) => {
            eprintln!("baton-detect: --since: {error}");
            return ExitCode::from(2);
        }
    };
    let defaults = Sources::defaults();
    let sources = Sources {
        claude_dir: args.claude_dir.or(defaults.claude_dir),
        codex_dir: args.codex_dir.or(defaults.codex_dir),
        opencode_db: args.opencode_db.or(defaults.opencode_db),
        claude_status: args.claude_status,
    };
    let mut collector = Collector::new(sources, since, BufWriter::new(io::stdout().lock()));
    let result = if args.once {
        collector.scan().and_then(|()| collector.finish())
    } else {
        collector.follow(
            Duration::from_millis(args.poll_ms),
            Duration::from_secs(args.rescan_secs),
        )
    };
    match result {
        Ok(()) => ExitCode::SUCCESS,
        // batond stopped reading: nothing left to do.
        Err(error) if error.kind() == io::ErrorKind::BrokenPipe => ExitCode::SUCCESS,
        Err(error) => {
            eprintln!("baton-detect: {error}");
            ExitCode::FAILURE
        }
    }
}

/// Claude Code waits for this on every refresh, so it reads little and writes one line at
/// most; whatever goes wrong goes to stderr, which Claude Code does not show.
fn statusline(args: &StatuslineArgs) -> ExitCode {
    let mut input = String::new();
    // A payload is a few kilobytes; more is not Claude Code's.
    let read = io::stdin().lock().take(1 << 20).read_to_string(&mut input);
    let payload: serde_json::Value = match read.map(|_| serde_json::from_str(&input)) {
        Ok(Ok(payload)) => payload,
        _ => return ExitCode::SUCCESS,
    };
    if let Some(line) = claude_status::record(&payload, time::OffsetDateTime::now_utc())
        && let Err(error) = append_if_changed(&args.log, &line)
    {
        eprintln!("baton-detect: {}: {error}", args.log.display());
    }
    let text = claude_status::status_text(&payload);
    if !text.is_empty() {
        println!("{text}");
    }
    ExitCode::SUCCESS
}

/// Appends `line` unless the log's last line reports the same windows. One `write` of a
/// short line with `O_APPEND`, so sessions writing at once do not interleave.
fn append_if_changed(log: &Path, line: &str) -> io::Result<()> {
    if last_line(log)?.is_some_and(|last| claude_status::same_windows(&last, line)) {
        return Ok(());
    }
    if let Some(dir) = log.parent() {
        fs::create_dir_all(dir)?;
    }
    let mut file = OpenOptions::new().create(true).append(true).open(log)?;
    file.write_all(format!("{line}\n").as_bytes())
}

fn last_line(log: &Path) -> io::Result<Option<String>> {
    let mut file = match fs::File::open(log) {
        Ok(file) => file,
        Err(error) if error.kind() == io::ErrorKind::NotFound => return Ok(None),
        Err(error) => return Err(error),
    };
    let length = file.metadata()?.len();
    file.seek(SeekFrom::Start(length.saturating_sub(4096)))?;
    let mut tail = String::new();
    file.read_to_string(&mut tail)?;
    Ok(tail.lines().last().map(str::to_owned))
}
