---
status: accepted
date: 2026-10-03
---

# Project name: baton-herdr, and its name layers

## Context and Problem Statement

The project was developed as **coban** and published at `github.com/selorito/baton-herdr`.
The name has to be the same everywhere a user meets it, and each layer has its own
constraints: `baton` is taken on PyPI, an import package cannot contain a hyphen, and a
command should be short.

## Decision Outcome

| Layer | Name |
|-------|------|
| Repository, PyPI distribution | `baton-herdr` (free on PyPI; plain `baton` is taken) |
| Python import package | `baton_herdr` (`src/baton_herdr`); import-linter contracts follow |
| CLI command | `baton` |
| Daemon | `batond` (was cobanD) |
| systemd user units | `baton.service`, `baton-herdr.service` |
| herdr workspace and session | `baton`; live tests use `baton-smoke-<pid>` |
| Settings and data | `~/.config/baton/baton.toml` and `.env`, `$XDG_DATA_HOME/baton/baton.db`; environment prefix `BATON_` (`COBAN_CONFIG` becomes `BATON_CONFIG`) |
| Example settings | `baton.example.toml`; a local `baton.toml` is ignored by git |
| Agent markers | `[[BATON:END status=…]]`, `[[BATON:QUESTION]]` |
| Evidence prefix in the event log | `baton:` (was `coban:`) |
| Rust crate | `crates/baton-detect`, still a stub (ADR 0007) |

Unchanged on purpose:

- **ADRs 0001–0009 and the devlogs** keep the old name: they record what was decided and
  done at the time.
- **`fixtures/`** keeps its captures as recorded, including paths in the old sandbox
  `~/dev/coban-sandbox`. The fixtures audit therefore allows any sandbox named
  `~/dev/<name>-sandbox` instead of one fixed name; new captures use `~/dev/baton-sandbox`.

### Consequences

- Good: one name per layer, all derived from `baton`; the PyPI name is available.
- The project used to be called coban; no installs were distributed under that name, so
  there is no migration tool.
- Event logs written under the old name carry `coban:` evidence, which the new code does
  not recognise.
