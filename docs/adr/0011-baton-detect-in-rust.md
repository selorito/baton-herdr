---
status: accepted
date: 2026-10-03
---

# baton-detect is written in Rust, for reasons other than speed

Partially supersedes [ADR 0007](0007-detector-in-python-first.md): its decision that the
detector stays in Python, and that Rust waits for a benchmark. ADR 0007's NDJSON contract and
its tests against `fixtures/` still hold.

## Context and Problem Statement

ADR 0007 kept the detector in Python because the only argument for Rust was performance, and
herdr already does the hot path. That is still true. But there are other reasons for a
separate Rust binary that ADR 0007 did not weigh, and roadmap step 2 (quota accounting) adds
a second detector job: reading the agents' usage logs as they grow.

Where should `baton-detect` live, and how does it replace the Python detector without a
risky switch-over?

## Decision Drivers

- (a) **One long-running binary, independent of the Python service.** Following log files and
  a SQLite database is a process of its own; it should keep its own state, restart on its own
  and be usable without batond (for example from `baton doctor` or by hand).
- (b) **Alignment with herdr.** herdr's agent-detection rules are TOML with a matching engine
  in Rust (Apache-2.0). A Rust detector can use the same format and matching logic, so rules
  baton needs can later be offered upstream. Code taken from herdr keeps its license header
  and is credited in a NOTICE file.
- (c) **Type safety in parsers.** Usage logs and screen rules are untrusted, versioned formats
  that change without notice; serde types make every assumption explicit and every deviation
  an error that is reported, not a silent wrong number.
- Changing the detector must never put task supervision at risk.

## Decision Outcome

`baton-detect` is implemented in Rust (`crates/baton-detect`).

**Migration: strangler fig.**

1. Rust implements the same NDJSON contracts (`schemas/`).
2. Python and Rust run over the same fixtures and their outputs are compared (parity tests).
3. Once they match, a setting (`detector = "python" | "rust"`) switches batond to Rust.
4. Then the Python detector is removed.

**Phase 1: the usage collector.** `baton-detect usage` follows Claude Code and Codex JSONL
logs and OpenCode's SQLite database and writes one NDJSON line per usage record and per Codex
rate-limit observation (`schemas/usage-event.v1.json`). There is no Python usage reader, so
its parity test checks the binary's output against the schema and the golden files instead.
The screen classifier is phase 2.

**Usage data is kept apart from the task event log.** Task events change state: projections
fold them into the board, and every one of them belongs to a task. Usage records are
telemetry: they belong to an agent session, arrive in volume, and are only ever summed. They
go into their own append-only tables (`usage_records`, `rate_limit_observations`), where a
`UNIQUE` record id lets the database drop duplicates (`INSERT … ON CONFLICT DO NOTHING`). The
task event model and its invariants do not change.

### Consequences

- Good: usage collection runs and restarts on its own; a crash in it does not touch task
  supervision.
- Good: a path to sharing detection rules with herdr.
- Bad: two toolchains in the product, not only in the repository; installing baton now also
  means building `baton-detect` (`cargo install`).
- Bad: during the migration, detection logic exists twice; the parity tests keep the copies
  from drifting.
