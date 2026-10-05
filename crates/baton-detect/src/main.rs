//! `baton-detect`: reports what coding agents leave behind to batond as NDJSON
//! (ADR 0011). Phase 1: `baton-detect usage`; phase 2: `baton-detect classify`.

use std::io::{self, BufRead, BufWriter, Write};
use std::path::PathBuf;
use std::process::ExitCode;
use std::time::Duration;

use baton_detect::classify::{Request, classify};
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
    Classify,
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
        Some(Command::Classify) => match classify_lines(io::stdin().lock(), io::stdout().lock()) {
            Ok(()) => ExitCode::SUCCESS,
            Err(error) if error.kind() == io::ErrorKind::BrokenPipe => ExitCode::SUCCESS,
            Err(error) => {
                eprintln!("baton-detect: {error}");
                ExitCode::from(2)
            }
        },
    }
}

/// The detector's NDJSON contract, the same as `baton detect`: a request that cannot be
/// read, or cannot be classified (an unknown time zone), stops with exit status 2.
fn classify_lines(input: impl BufRead, mut output: impl Write) -> io::Result<()> {
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
        let result = classify(&request).map_err(|e| invalid(e.to_string()))?;
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
