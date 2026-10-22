---
status: accepted
date: 2026-10-21
---

# Budget with parallel sessions: reserve what running attempts are expected to spend

## Context and Problem Statement

ADR 0012 reads each agent's remaining budget at the moment a task is assigned; ADR 0017
lets several sessions of one agent run at once. Usage limits are an account's, not a
session's: Claude's 5-hour and weekly windows and Codex's quota are shared by every session
signed in with that account, baton's and the user's own alike.

What baton reads is already account-wide:

- Codex's `rate_limits` and Claude's status line (ADR 0015) are the account's own figures,
  written by every session.
- The Claude estimate sums the tokens of every Claude session logged on the machine.

On one machine each agent runs under one login (`~/.claude`, `~/.codex`), so in baton
"agent" and "agent's account" are the same. More than one account per agent is not
supported, and would need the account as part of the unit.

What is missing is time. Those figures move only after responses have been written. Four
tasks assigned in the same second all see the same figure, even though none of them has
spent anything yet. With 12 % left and a 10 % reserve, all four start on the same agent,
and all four can run into its limit within minutes. That costs four interruptions and four
hand-offs, where one task would have cost one.

How should the choice of agent account for what running attempts are about to spend,
without a race between parallel starts and without keeping state beside the event log
(ADR 0002)?

## Decision Drivers

- Availability and budget stay folds of the event log and the usage tables.
- Budget still never refuses work alone (ADR 0012): it reorders, and below the reserve it
  slows down.
- An expectation must never pass for a measurement (ADR 0012): estimates say they are.
- Choices for parallel starts must not race: each one sees the reservations made before it.

## Considered Options

1. Leave it. Let agents hit the limit and hand off (ADR 0005).
2. Count running attempts against the agent with a fixed share (say 5 % each).
3. Reserve each running attempt's expected cost, learned from past attempts, minus what it
   has already spent; read the reservations under the same lock that starts attempts.

## Decision Outcome

Chosen option: **3**.

- **Expected cost.** For each agent and kind of task (`plan`, `step`, `merge`, `task`), the
  median of the counted tokens (ADR 0012: input + cache writes + output) of the last 20
  finished attempts. A finished attempt's tokens are its session's records, joined to the
  attempt by its `session_ref`. An attempt with no records is left out rather than counted
  as free. With fewer than 3 such attempts, 45k tokens: the mean of the 19 Claude sessions
  measured on 2026-10-18. An attempt's expected cost changes with the median; nothing is
  stored. Only the records the budget reads anyway (the last 24 hours, or back to the
  last Claude limit) are used, so older attempts drop out of the median.
- **Reservation.** For a running attempt: what it is expected to cost, less what its
  session has spent so far, and never below zero. The part it has already spent is in the
  reported figure, so it is not counted twice. The figure catching up with what the attempt
  spends is the correction. When the attempt ends, its reservation goes.
- **In percent.** Reported budgets are shares, so tokens are turned into a share with the
  agent's tokens per percent in its current window. That rate is the counted tokens since
  the window opened divided by the share it reports used, learned once at least 5 % is
  used. A window that opened before the records read (a weekly one, usually) cannot be
  measured that way. For the Claude estimate, the share comes from the cap. With no rate
  the reservation is unknown, and the budget stays as it is, as it does today.
- **Effective remaining** = remaining less the reservations of the agent's running
  attempts. `choose_agent` compares this figure with the reserve. Notices show it as an
  estimate ("~8 % left after 3 running").
- **Below the reserve, one at a time.** An agent whose effective remaining is below the
  reserve takes one session at a time, whatever its `agent_slots`. Only one task then runs
  into a limit that is close. The dispatcher counts free slots with the same figure, so it
  does not start workers for slots that budget has taken away.
- **No race.** The dispatcher already chooses and starts attempts under one lock (ADR
  0017). The choice reads the running attempts from the log inside that lock, so the second
  start of a tick sees the first one's reservation.

### Consequences

- Good: parallel starts stop piling onto an agent that is about to hit its limit, and the
  reason line says why a task went to the second agent.
- Good: no new events and no counters. A restart of batond rebuilds the same reservations.
- Bad: the median is rough. Tasks differ by ten times or more, so a large task can still hit
  a limit. Its cost stays what ADR 0012 accepts: one limit and a hand-off.
- Bad: the dispatcher reads the budget at every tick, as agent choice did at every
  assignment. This is one query on the usage tables. Sessions are matched in memory, so
  no new index is needed.
- Bad: tokens are counted on this machine only. Claude's status line reports the whole
  account, so sessions on other machines make tokens per percent look smaller. Each
  reservation then looks larger, which errs on the side of caution.
- Not covered: tokens spent by the user's own sessions after a choice. They show up in the
  next reported figure, as they do today.
- Not covered: Codex sessions refreshing one token at the same time during a run
  (CHANGELOG 1.2.0, known limitations). That is a sign-in matter, not a budget one.
