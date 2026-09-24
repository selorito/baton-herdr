# coban

coban is an orchestrator for coding agents (Claude Code, Codex CLI, Gemini CLI, OpenCode)
running inside [herdr](https://github.com/herdrdev/herdr). It watches each agent through
herdr's socket API and plugin system, resumes work an agent left unfinished, and assigns new
tasks based on how much token quota each agent has left. The orchestrator service (cobanD) is
written in Python. A small Rust binary (coban-detect) detects agent state from screen
snapshots. Every change of state is recorded in an append-only event log.

**Status: pre-alpha.** Only the project skeleton exists; nothing is usable yet.

## Development

Requirements: [uv](https://docs.astral.sh/uv/), a stable Rust toolchain (via rustup) and
[just](https://just.systems/).

```bash
uv sync                  # create .venv with Python 3.12 and all dependencies
just check               # lint, type-check, import contracts and tests (Python and Rust)
just fmt                 # format everything
just test                # run tests only

uv run coban version
cargo run -p coban-detect -- --version
```

Configuration: copy `coban.example.toml` to `coban.toml` (or set `COBAN_CONFIG`). Put secrets
in `.env` (see `.env.example`). Any setting can be overridden with `COBAN_<SECTION>__<KEY>`.

Architecture decisions are in [docs/adr/](docs/adr/). Contributor and agent guidelines are in
[CLAUDE.md](CLAUDE.md).

## License

Apache-2.0. See [LICENSE](LICENSE).
