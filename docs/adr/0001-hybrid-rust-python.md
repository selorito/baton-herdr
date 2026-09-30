---
status: superseded by ADR 0007
date: 2026-09-24
---

# Hybrid Rust + Python architecture

> **Superseded by [ADR 0007](0007-detector-in-python-first.md)** for the detector's
> implementation language: the detector is written in Python first. The separation of the
> detector behind an NDJSON contract defined by JSON Schema, decided here, still holds.

## Context and Problem Statement

coban has two kinds of work with very different profiles:

- **Orchestration**: task state, retries, quota accounting, an HTTP API, a Telegram bot, a
  database. This is I/O-bound, changes often while the product is being discovered, and
  benefits from a large library ecosystem.
- **Detection**: continuously reading terminal screen snapshots of several agents, matching
  them against patterns, and watching files. This runs per snapshot × pane, must stay cheap as
  the number of panes grows, and must not stall when the orchestrator is busy.

Which language(s) should each part use, and how should they talk?

## Decision Drivers

- Speed of iteration on orchestration logic.
- Predictable latency and low CPU for detection with many panes.
- Mature libraries for HTTP APIs, SQL, migrations and Telegram.
- A boundary that can be tested on each side in isolation.

## Considered Options

1. Python only.
2. Rust only.
3. Python orchestrator (cobanD) + Rust detector (coban-detect), talking over subprocess
   stdin/stdout with NDJSON and a JSON Schema contract.
4. Python orchestrator + Rust detector as a native extension (PyO3).

## Decision Outcome

Chosen option: **3**. cobanD is Python 3.12 (FastAPI, Pydantic v2, SQLAlchemy 2.0 async +
aiosqlite, Alembic, aiogram 3, structlog). coban-detect is a Rust binary (tokio, serde, notify,
regex, clap). cobanD starts coban-detect as a child process; they exchange newline-delimited
JSON. Every message type is described by a JSON Schema kept in the repository, and both sides
are tested against that schema.

### Consequences

- Good: each side uses the ecosystem that fits its job.
- Good: the process boundary isolates failures. A detector crash does not take down cobanD;
  cobanD restarts it.
- Good: the NDJSON contract can be tested with recorded fixtures on either side, and
  coban-detect can be run by hand for debugging.
- Bad: two toolchains, two CI jobs, two sets of conventions.
- Bad: the contract must be kept in sync. Mitigation: the JSON Schema is the single
  definition, and a mismatch fails tests on both sides.
- Bad: serialization cost on every message. Acceptable because detection results are small
  and far less frequent than raw screen updates.

## Pros and Cons of the Options

### Python only

- Good: one toolchain, fastest to start.
- Bad: pattern matching over many panes competes with the orchestrator for the GIL and the
  event loop, so latency is hard to predict.

### Rust only

- Good: best performance, one language.
- Bad: slower iteration on product logic that is still changing; a smaller ecosystem for bots
  and admin tooling.

### Python + PyO3 extension

- Good: no serialization or process management.
- Bad: a detector panic or deadlock affects the orchestrator process; building wheels across
  platforms complicates packaging; the boundary is harder to observe and test from outside.
