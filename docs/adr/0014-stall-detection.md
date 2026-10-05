---
status: accepted
date: 2026-10-05
---

# A working agent is stuck when neither its screen nor its tokens move

## Context and Problem Statement

A hung agent shows no screen of its own. A frozen process, or a request that never returns,
keeps showing whatever it showed last, usually "working". Until now baton caught that only
when the turn timeout ran out (`turn_timeout_seconds`, one hour after the prompt). The
benchmark measured a median of 53 minutes from the hang to recovery.

A shorter timeout is not the answer. Turns legitimately run for hours, and a model may think
for minutes without printing anything. What tells a stuck agent from a slow one?

## Decision Drivers

- No false alarms on long responses. Resuming a busy agent throws its current response
  away.
- Use signals baton already has: the screen it reads every two seconds, and the usage
  records the collector writes for every response (ADR 0011).
- Every interruption explainable afterwards from the event log (ADR 0002).

## Considered Options

1. A shorter turn timeout.
2. The screen alone: no change for N minutes.
3. Two signals together: no screen change and no tokens recorded for the session, for N
   minutes.

## Decision Outcome

Chosen option: **3**, with the screen alone as a fallback when usage is not collected.

**Progress** (`scheduler/stall.py`) is either:
- **The screen changes.** Before comparing, what moves on its own while a request is
  pending is removed:
  - elapsed-time counters (`3m 12s`, `2s`);
  - clocks;
  - running token counts (`12.4k tokens`);
  - spinner frames;
  - animated ellipses.

  Other numbers are kept, so "Step 2 of 9" after "Step 1 of 9" is progress.
- **Tokens are recorded for the attempt's session.** One record per completed response. If
  the records carry no session id that matches, every record of the agent counts. That can
  only delay detection, never cause it.

**The rule** (`TaskRunner._no_progress`): while the agent is `working` after the prompt,
and neither signal has moved for `[scheduler] stall_minutes` (default **15**), the attempt
is interrupted as `stalled`. Recovery then follows ADR 0005: the session is resumed, up to
`max_failure_resumes`.

- **Without `[usage]`**, the screen alone decides, after `stall_minutes_without_usage`
  (default **30**).
- **Waiting for a person** or **idle** does not count: the clock starts again at the next
  `working`.
- **The turn timeout stays** as the last backstop.

**Why 15 minutes.** A response that is legitimately in progress shows no tokens until it
ends, so the threshold must be longer than one response can take. The agents bound that
themselves:
- Claude Code times an API request out after 10 minutes (`API_TIMEOUT_MS`, default 600000,
  code.claude.com/docs/en/env-vars);
- Codex drops a stream that is idle for 5 minutes (`DEFAULT_STREAM_IDLE_TIMEOUT_MS` = 300000
  in `codex-rs/model-provider-info`).

Past 15 minutes with neither a screen change nor a completed response, the agent's own
timeout should already have fired. Something outside the request is stuck: the process, the
terminal, or the network after the timeout. Without token records, a long response and a
hang look the same on screen for longer, so the wait doubles.

**Recorded with its reason.** `attempt.interrupted` has a new optional `detail`, for
example "Working for 15 min with no change on screen and no tokens recorded for its
session." The same text goes into the "stopped" notice.

### Consequences

- Good: the benchmark's hang recovers in about 15 minutes instead of about 53
  (`bench/results/`). Its long-response scenario, single responses of 5–10 minutes with a
  still screen, raised no false alarm.
- Good: no new I/O. The screen is already read every poll, and usage is read only once the
  screen has been still for the whole threshold.
- Bad: an agent version that raises its request timeout above 15 minutes (`API_TIMEOUT_MS`
  can be set higher) needs `stall_minutes` raised with it.
- Bad: the screen filter is a list of known moving parts. A screen that changes on its
  own in some other way, such as a progress bar while hung, hides a stall until the turn
  timeout. That is no worse than before.
- Bad: matching tokens to the attempt by session id depends on the integration reporting
  it. Without one, any session of the same agent counts as progress.

## Pros and Cons of the Options

### 1. A shorter turn timeout

- Good: trivial.
- Bad: it ends long legitimate turns. It measures the turn, not progress.

### 2. The screen alone

- Good: works without usage collection.
- Bad: a model thinking for minutes shows a still screen. The threshold must be long, or it
  raises false alarms. It is kept as the fallback, with the longer threshold.

### 3. Screen and tokens

- Good: each signal covers the other's blind spot. Tokens arrive while output scrolls past
  unchanged regions, and the screen changes during a long response before its tokens are
  logged.
- Bad: two signals to keep right instead of one.
