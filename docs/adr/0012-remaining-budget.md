---
status: accepted
date: 2026-10-03
---

# Remaining budget per agent: reported for Codex, estimated for Claude, never a hard gate

## Context and Problem Statement

Roadmap step 2 collects every response's tokens and Codex's own usage windows into
`usage_records` and `rate_limit_observations` (ADR 0011). The scheduler still picks agents
only by preference order and by limits an agent has already hit. How much of each agent's
budget is left, and how should that steer which agent takes a task?

The agents do not report the same things:

- **Codex** writes `rate_limits` into its session logs: for each window (5 hours, a week) the
  share used and when it resets. That is the agent's own figure.
- **Claude Code** writes tokens per response but nothing about its limits: neither the cap
  nor the share used. Its session limit is a 5-hour window that opens with the first message
  (code.claude.com/docs/en/costs); the cap depends on the plan and the model and is not
  published as a token count.
- **OpenCode** uses whichever provider is configured; there is no limit to read.

## Decision Drivers

- An estimate must never pass for a measurement: everything shown or logged says where its
  figure came from.
- A wrong estimate must be cheap. Agents announce their limit on screen, and baton already
  recovers from that (ADR 0005); the budget should avoid limits, not replace that path.
- Every scheduling decision must be explainable afterwards from the event log (ADR 0002).

## Decision Outcome

**One budget per agent** (`budget.quota`), with its source:

| Agent | Source | Remaining |
|-------|--------|-----------|
| Codex | `reported`: its latest `rate_limits` (within 8 days) | 100 − the highest `used_percent` across windows; a window whose reset time has passed counts as unused |
| Claude Code | `configured` or `learned`: **an estimate** | 100 − used ÷ cap of the current 5-hour window |
| OpenCode, or no data | `unknown` | not given |

**Claude's estimate.**

- The window is worked out from the recorded responses. It opens with the first response
  outside the previous window and lasts 5 hours. Responses before the last known reset are
  ignored.
- Tokens are counted as `input + cache_write + output`. Cache reads are left out: they cost a
  fraction of the rest and would swamp it. The cap is counted the same way, so the choice
  only has to be consistent, not exact.
- The cap comes from one of two places, in this order:
  - `[budget] claude_window_tokens`, set by the operator;
  - otherwise it is learned from the last session limit baton saw: the tokens spent in the
    window that ended with that limit. A limit counts as a session limit only when it printed
    a reset time within 5 hours. Weekly and spend limits say nothing about the session cap.
- With neither, Claude's budget is `unknown`. baton still shows the tokens used in the
  current window and when it resets.
- What it does not model:
  - weekly limits;
  - differences between models (Opus spends a window faster than Sonnet);
  - use of the same account outside this machine.

  Any of these makes the estimate optimistic, which is why it is labelled an estimate
  everywhere: `~62% left (estimate …)` in `baton budget` and Telegram's `/budget`, and
  `estimate: true` in the event log.

**Budget reorders and never excludes** (`budget.choice`). The configured agent order stays
the preference:

- An agent with less than `[budget] reserve_percent` (default 10) left goes behind every
  agent that has more, or whose budget is unknown.
- If every agent that could take the task is short, the one with the most left goes first.
- Unknown is never "low".
- Only a limit the agent actually hit makes it unavailable (`budget.availability`, unchanged).

A low budget therefore never parks a task. The daemon's wake-up and re-run logic only follows
limits and new events, not a budget that drains or refills. The worst outcome of a wrong
figure is one more limit, which baton already handles.

**Every choice is an event.** Before an attempt starts, the scheduler appends
`task.agent_chosen`:

- `agent` is the agent chosen, or `null` when none can take the task;
- `reason` is the decision in words, for example `claude: next in preference order (codex 4%
  left, below the 10% reserve), budget unknown.`;
- `budgets` holds every considered agent's standing: available, remaining percent, estimate.

The event belongs to the task's log because it explains that task's next attempt. It carries
only the summary the decision used. The usage records stay in their own tables (ADR 0011).

### Consequences

- Good:
  - Codex work moves elsewhere before Codex hits its limit.
  - The log says why each agent was picked.
  - `baton budget` shows the same reasoning for the next task.
- Good: no new failure mode for tasks. Without usage data the budget is `unknown`, and
  scheduling is exactly what it was.
- Bad: Claude's figure is a guess until it has hit a session limit under baton, or the
  operator configures a cap. On a fresh install it shows `budget unknown`.
- Bad: Claude Code's status line receives `rate_limits` (session and weekly, used share and
  reset time). Reading that would replace the estimate with Claude's own figures, but it
  needs a status-line command installed in the operator's Claude Code settings. Left for
  later; the `reported` source is ready for it.
