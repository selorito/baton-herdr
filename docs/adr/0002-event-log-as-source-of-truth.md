---
status: accepted
date: 2026-09-24
---

# Append-only event log as the single source of truth

## Context and Problem Statement

coban's core promise is that agent work is not lost halfway: after a crash, a quota
exhaustion, or a restart of herdr or cobanD, it must know exactly what each task and attempt
was doing and continue from there. Several views of that state are needed: the scheduler's
queue, the budget tracker's per-agent usage, the API and web panel, Telegram notifications.

How should coban store state so it can be recovered and explained?

## Decision Drivers

- Crash recovery must be correct without guessing.
- Every decision (why an agent was chosen, why a task was retried) must be auditable.
- Several read models with different shapes.
- Deterministic tests: replaying the same events must give the same state.

## Considered Options

1. Mutable tables for the current state (classic CRUD).
2. Mutable tables plus a separate audit log.
3. An append-only event log as the only source of truth; all other state is a projection
   derived from it.

## Decision Outcome

Chosen option: **3**. Every state change is written as an immutable, ordered event
(for example "task created", "attempt started", "agent reported quota", "attempt
interrupted"). Events are never updated or deleted. Current state (task status, agent budget,
queues) is a projection built by folding events. Projections may be cached in tables for speed,
but they can always be dropped and rebuilt from the log. Only the `ledger` package reads and
writes the log.

### Consequences

- Good: recovery is replay. After a crash, cobanD rebuilds state from the log instead of
  inferring it.
- Good: a full history for debugging and for explaining scheduling decisions.
- Good: projections are pure functions of events, so they are easy to unit- and
  property-test.
- Good: new read models can be added later by replaying existing history.
- Bad: event schemas are a long-term contract. Changing an event type needs versioning or
  upcasting.
- Bad: projections must be kept in step with the log; a bug in a projection needs a rebuild.
- Bad: the log grows without bound. Snapshots and compaction are future work.

## Pros and Cons of the Options

### Mutable tables (CRUD)

- Good: simplest, familiar.
- Bad: history is lost on every update; after a crash in the middle of a multi-step change,
  the true state is ambiguous.

### Mutable tables plus audit log

- Good: some history.
- Bad: two sources of truth that can disagree; the audit log is usually incomplete exactly
  where it matters.
