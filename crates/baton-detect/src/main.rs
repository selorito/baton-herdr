//! `baton-detect`: reads agent screen snapshots and reports agent state to
//! batond as NDJSON. Only the command-line surface exists so far.

use clap::Parser;

/// Screen-state detector for coding agents running in herdr panes.
#[derive(Debug, Parser)]
#[command(name = "baton-detect", version, about)]
struct Cli {}

fn main() {
    let _cli = Cli::parse();
}
