# coban

coban is an orchestrator for coding agents (Claude Code, Codex CLI and OpenCode in v1)
running inside [herdr](https://github.com/herdrdev/herdr). It watches each agent through
herdr's socket API and plugin system, resumes work an agent left unfinished, and assigns new
tasks based on how much token quota each agent has left. It is one Python process (cobanD)
with a CLI and a Telegram bot. Every change of state is recorded in an append-only event log.

**Status: pre-alpha.** The foundations exist (event log, herdr client); nothing is usable
yet. See [docs/ROADMAP.md](docs/ROADMAP.md).

## Development

Requirements: [uv](https://docs.astral.sh/uv/), a stable Rust toolchain (via rustup) and
[just](https://just.systems/).

```bash
uv sync                  # create .venv with Python 3.12 and all dependencies
just check               # lint, type-check, import contracts and tests (Python and Rust)
just fmt                 # format everything
just test                # run tests only
just smoke               # optional: live test against an installed herdr

uv run coban version
cargo run -p coban-detect -- --version
```

`just smoke` needs the `herdr` binary on `PATH`. It starts its own headless herdr server in
a throwaway named session (its own socket), drives a shell pane, then stops the server and
deletes the session. It never touches a herdr session you have open, starts no agent, and is
not part of `just check` or CI.

Configuration: copy `coban.example.toml` to `coban.toml` (or set `COBAN_CONFIG`). Put secrets
in `.env` (see `.env.example`). Any setting can be overridden with `COBAN_<SECTION>__<KEY>`.

Architecture decisions are in [docs/adr/](docs/adr/). Contributor and agent guidelines are in
[CLAUDE.md](CLAUDE.md).

## License

Apache-2.0. See [LICENSE](LICENSE).
