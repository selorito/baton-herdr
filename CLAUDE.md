# baton

Orchestrator that runs on top of [herdr](https://github.com/herdrdev/herdr). It keeps coding
agents (Claude Code, Codex CLI, OpenCode; Gemini CLI in v1.1) working until their tasks are
finished and distributes work according to each agent's remaining token quota.

Status: MVP (usable unattended on one machine). See `docs/ROADMAP.md` for what exists and
what is next, and ADR 0008 for the scope of v1.

## Architecture

- **batond** (`src/baton_herdr/`): one Python 3.12 process. Pydantic v2, SQLAlchemy 2 async +
  aiosqlite, Alembic, structlog; aiogram 3 for the Telegram bot, which runs inside batond.
- **Detector**: Rust, `crates/baton-detect` (ADR 0011), behind NDJSON contracts defined by
  JSON Schema in `schemas/`: the usage collector (`baton-detect usage`) and the screen
  classifier (`baton-detect classify`, rules in `crates/baton-detect/rules/*.toml`). Its
  answers are pinned in `tests/golden/classify.jsonl`; a deliberate rule change
  regenerates them with `UPDATE_GOLDEN=1`, reviewed in the diff.
- **Not in v1** (ADR 0008): web panel, REST API, Gemini CLI. `baton_herdr.api` is an empty
  placeholder.
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

- **core-is-pure**: `baton_herdr.core` may not import any other baton package.
- **layers**: interfaces → services → infra → core. A higher layer may import a lower one,
  never the reverse. The contract is `exhaustive`, so a new top-level package under `baton/`
  fails the check until it is assigned a layer. Within interfaces and within services,
  packages may import each other (`:`). Infra packages are independent of each other (`|`):
  they meet only through protocols in `core`.
- **adapters-independent**: modules under `baton_herdr.adapters` may not import each other. Shared
  adapter code belongs in `core` (as a protocol or pure helper).

- **only-ledger-touches-the-database**: no package except `baton_herdr.ledger` may import
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
just smoke     # live test against a real herdr in a throwaway session (not in check or CI)
uv run baton version
cargo run -p baton-detect -- --version
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
- **Tests that need a real herdr carry `@pytest.mark.live`.** They are excluded from the
  default run and CI, run with `just smoke`, must start their own named herdr session, and
  must never open, read or close panes in a session the developer has open.
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
- Logging: `baton_herdr.core.logging.get_logger(__name__)`; JSON lines. Bind `task_id` /
  `attempt_id` with `log_context(...)` instead of passing them into every call.
- Secrets: `SecretStr` fields in `core/config.py`, loaded from `.env` or the environment,
  never from committed files, never logged. `.env` and `baton.toml` are git-ignored.
- Rust: edition 2024, `cargo fmt`, clippy pedantic with `-D warnings`, no `unsafe`, no
  `unwrap`/`expect` outside tests.

## Layout

```
src/baton_herdr/        Python package (batond + CLI)
crates/           Rust: baton-detect (usage collector, screen classifier)
herdr-plugin/     herdr plugin (placeholder)
tools/fake-agent/ scripted fake agent for tests (placeholder)
fixtures/<agent>/ captured screens per agent
docs/adr/         architecture decision records
docs/devlog/      development log
tests/            unit, property, integration
```
