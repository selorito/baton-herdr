---
status: accepted
date: 2026-10-20
---

# The operator can release an agent baton set aside

## Context and Problem Statement

baton sets an agent aside after it hits a usage limit, until the time the agent printed,
or for `limit_cooldown_minutes` when it printed none (ADR 0012), and since 1.2 after it
asks to be signed in (`signed_out`, for the cooldown). The time is a guess when no reset
was printed, and a sign-in is fixed by a person, usually in minutes. Live, Codex was set
aside for an hour after its sign-in screen; the operator signed it in and stopped its stale
app server within minutes, and had to wait out the hour. How does the operator tell baton
that an agent can be used again, without breaking the event log as the only source of
truth (ADR 0002)?

## Decision Drivers

- Availability stays a fold of the event log; nothing is kept beside it.
- Every event belongs to a task (ADR 0002); an agent has no stream of its own.
- A release is an operator's statement, recorded with who made it (ADR 0009).

## Considered Options

1. Edit the configuration or restart batond to forget the limit.
2. An event about the agent, outside any task.
3. A note on the attempt whose interruption set the agent aside: `agent.released`.

## Decision Outcome

Chosen option: **3**.

`baton agent ready <agent>` finds the newest interruption that set the agent aside (a
limit or a sign-in) and appends `agent.released` for that attempt, with `by`. The
availability fold clears the agent's wait on it; a later limit or sign-in sets it aside
again as before. When the agent is not set aside, nothing is written and the command says
so. The event is a note, allowed on a task that has closed since, like the plan notes of
ADR 0016. batond is woken, so waiting tasks may start at once.

### Consequences

- Good: the agent is back in use as soon as a person has fixed it, and the log says who
  said so and when.
- Bad: a release given too early (the limit has not reset) costs one more limit, which
  sets the agent aside again with its printed reset time.
- Not yet: the same action from Telegram.
