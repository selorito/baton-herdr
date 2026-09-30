# coban

Orchestrator that runs on top of [herdr](https://github.com/herdrdev/herdr). It keeps coding
agents (Claude Code, Codex CLI, Gemini CLI, OpenCode) working until their tasks are finished
and distributes work according to each agent's remaining token quota.

Status: pre-alpha. Only the M0 skeleton exists.

## Architecture

- **cobanD** (`src/coban/`): Python 3.12 orchestrator service. FastAPI, Pydantic v2,
  SQLAlchemy 2.0 async + aiosqlite, Alembic, aiogram 3, structlog.
- **coban-detect** (`crates/coban-detect/`): Rust binary that reads agent screen snapshots and
  reports agent state. tokio, serde, notify, regex, clap.
- **web** (`web/`): React + Vite + TypeScript panel. Not started.
- Rust ↔ Python: cobanD runs coban-detect as a subprocess and they exchange NDJSON. Every
  message type is defined by a JSON Schema; the schema is the contract, not either codebase.
- herdr is used only through its socket API / CLI and its plugin system. herdr is never forked
  (ADR 0003).
- The append-only event log is the single source of truth. Every other piece of state is a
  projection that can be rebuilt from it (ADR 0002).

See `docs/adr/` for the reasoning behind each decision.

## Module boundaries

Dependencies flow from the outside in. `core` knows nothing about any other package.

| Layer      | Packages                           | Responsibility                                         |
|------------|------------------------------------|--------------------------------------------------------|
| interfaces | `api`, `telegram`, `cli`           | Entry points for humans and external clients           |
| services   | `recovery`, `budget`, `scheduler`  | Orchestration use cases                                |
| infra      | `herdr`, `adapters`, `ledger`      | Talking to the outside world                           |
| core       | `core`                             | Domain model, protocols, config, logging. Pure.        |

Ownership rules:

- Only `herdr/` knows the herdr socket protocol.
- Agent-specific differences live only in `adapters/`. Adding a new agent means adding one
  module under `adapters/`; nothing else changes.
- Only `ledger/` touches the database.
- Every external dependency (herdr, Telegram, the clock, the file system) sits behind a
  `typing.Protocol` defined in `core`. Tests plug in fakes.

### Enforced by import-linter

`uv run lint-imports` (part of `just check`) enforces these contracts, configured in
`pyproject.toml` under `[tool.importlinter]`:

- **core-is-pure**: `coban.core` may not import any other coban package.
- **layers**: interfaces → services → infra → core. A higher layer may import a lower one,
  never the reverse. The contract is `exhaustive`, so a new top-level package under `coban/`
  fails the check until it is assigned a layer. Within interfaces and within services,
  packages may import each other (`:`). Infra packages are independent of each other (`|`):
  they meet only through protocols in `core`.
- **adapters-independent**: modules under `coban.adapters` may not import each other. Shared
  adapter code belongs in `core` (as a protocol or pure helper).

- **only-ledger-touches-the-database**: no package except `coban.ledger` may import
  `sqlalchemy`, `aiosqlite`, `alembic` or `sqlite3`.

If a contract breaks, fix the dependency direction. Do not loosen a contract without an ADR.

## Commands

Use `just` recipes rather than calling tools directly.

```bash
just check     # everything CI runs: ruff, mypy, import-linter, pytest, cargo fmt, clippy, cargo test
just check-py  # Python half of check
just check-rs  # Rust half of check
just fmt       # apply formatting and safe lint fixes
just test      # test suites only
uv run coban version
cargo run -p coban-detect -- --version
```

## Working rules

- **Protocol, fake and test first, then the real implementation.** Define the
  `typing.Protocol` in `core`, write a fake, write tests against the fake, and only then write
  the real adapter.
- **One module per branch.** A branch changes one package (plus tests and docs for it).
- **Dependencies are added in the session of the module that uses them.** Do not add a library
  "for later". `pyproject.toml`, `uv.lock`, `Cargo.toml` and `Cargo.lock` change only together
  with the code that imports the new dependency. Use the latest stable release and commit the
  lock files.
- **No merge with red CI.** `just check` must be green locally and in CI.
- **Conventional commits.** Lowercase type, optional scope, imperative subject, e.g.
  `feat(ledger): append events to sqlite`. Commits written with an AI agent end with a
  `Co-Authored-By:` trailer naming the model.
- **Architecture changes need an ADR** in `docs/adr/` (MADR format, next free number).
- Record notable session outcomes in `docs/devlog/YYYY-MM-DD-<slug>.md`.

## Code style

- Everything in English: code, comments, docs, commit messages.
- Python: ruff (lint + format, line length 100), mypy `strict`, absolute imports only,
  `from __future__ import annotations`. Pydantic models for data crossing a boundary.
- Tests: pytest, `asyncio_mode = "auto"`, hypothesis for property tests. Layout:
  `tests/unit/`, `tests/property/`, `tests/integration/`. Warnings are errors.
- Logging: `coban.core.logging.get_logger(__name__)`; JSON lines. Bind `task_id` /
  `attempt_id` with `log_context(...)` instead of passing them into every call.
- Secrets: `SecretStr` fields in `core/config.py`, loaded from `.env` or the environment,
  never from committed files, never logged. `.env` and `coban.toml` are git-ignored.
- Rust: edition 2024, `cargo fmt`, clippy pedantic with `-D warnings`, no `unsafe`, no
  `unwrap`/`expect` outside tests.

## Layout

```
src/coban/        Python package (cobanD + CLI)
crates/           Rust workspace members
web/              web panel (placeholder)
herdr-plugin/     herdr plugin (placeholder)
tools/fake-agent/ scripted fake agent for tests (placeholder)
fixtures/<agent>/ captured screens per agent
bench/            benchmarks
docs/adr/         architecture decision records
docs/devlog/      development log
tests/            unit, property, integration
```
