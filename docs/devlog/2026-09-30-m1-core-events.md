# 2026-09-30: M1 core event model

- ADR 0004 (agent state model on top of herdr's status) and ADR 0005 (automation boundaries),
  both derived from the H0 study.
- `core/model.py`: ids and enums (`AgentState`, `TaskStatus`, `InterruptReason`, …).
- `core/events.py`: eight immutable event types, discriminated by a `type` tag, plus
  `StoredEvent` (event + log position).
- `core/ports.py`: `EventStore` and `Clock` protocols, `ConcurrencyError`.
- `core/fakes.py`: `InMemoryEventStore`, `FixedClock`.
- `core/projection.py`: `project` / `apply`, a strict pure fold from events to a `Board`.
- Property tests generate arbitrary valid logs. They found a real inconsistency on the first
  run: ending an interrupted attempt left its interrupt reason set.

Next: `ledger` (SQLite `EventStore` with SQLAlchemy + Alembic), tested against the same
contract as the in-memory fake.
