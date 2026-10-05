# Roadmap

Order of work: **a thin vertical slice first, then deepen it.** Every step after the slice
keeps the end-to-end loop working and makes one part of it real. Scope is fixed by
[ADR 0008](adr/0008-v1-scope.md).

## Done

| Step | What exists |
|------|-------------|
| M0 skeleton | Repository layout, tooling (`just check`, CI definition), config, logging |
| H0 discovery | `docs/research/agents.md`, `fixtures/` for Claude Code, Codex, OpenCode (and Gemini first-run), capture tool with masking and audit |
| Core | Event model, `EventStore` and `PaneHost` protocols with fakes, projection, commands, targeting (ADR 0002, 0004, 0006) |
| Ledger | SQLite event store with migrations, append-only at the database level |
| herdr client | Socket client, herdr → baton state mapping, isolated live smoke test |

Slice 0 findings so far: herdr exposes an agent session id only from the official integration
source names (`herdr:claude`, …), and for Claude Code and Codex it takes the agent's identity
from the process name and its state from the screen. `fake-agent` therefore runs under the
agent's name and reports only its session, and herdr classifies its screens with the same
rules it uses for the real agent.

Not yet exercised: CI on GitHub (no remote has been added, so the workflow has never run).

## Slice 0: the thinnest end-to-end loop ✅

**Goal.** A simulated Claude hits its usage limit in the middle of a task; baton notices,
hands the task to Codex, and tells the user on Telegram. Everything is real except the
agents, which are played by `tools/fake-agent`.

```
task added ─► scheduler starts attempt on "claude" (fake)
                 │ fake prints a working screen, then Claude's limit message
                 ▼
          detector: rate_limited, reset time parsed
                 │
          recovery: interrupt attempt (reason, resume_not_before)
                 │
          scheduler: claude unavailable until reset ─► start attempt on "codex" (fake)
                 │ fake works, then shows its finished screen
                 ▼
          task completed ─► Telegram: "limit hit on Claude, moved to Codex", then "done"
```

Work items, each behind a protocol with a fake and tests first (✅ = done):

1. ✅ **`tools/fake-agent`**: a small terminal program that replays scripted screens taken from
   `fixtures/` (working, limit message, idle, done), so it can run in a real herdr pane or be
   stood in for by `FakePaneHost`.
2. ✅ **Detector, minimal**: request and response models with their JSON Schema (ADR 0007), and
   two rules: Claude's limit line with its reset time, and a finished or idle screen.
3. ✅ **Adapters, minimal**: for Claude and Codex, only what the slice needs: launch command,
   the two detector rules, prompt submission through targeting (ADR 0006).
4. ✅ **Budget, minimal**: an agent is unavailable from a `rate_limited` observation until its
   reset time. No token accounting yet.
5. ✅ **Scheduler and recovery, minimal**: one task at a time; on `rate_limited`, interrupt the
   attempt, then start a new attempt on an available agent, passing the task instructions.
6. ✅ **Notifier**: a `Notifier` protocol with a fake; a Telegram implementation that only sends
   messages (aiogram, inside batond).
7. ✅ **batond and CLI**: `baton task add`, `baton run`, `baton status`.

**Done when**

- an integration test drives the whole loop with `FakePaneHost`, the in-memory store and the
  fake notifier, and asserts the event log: attempt on Claude interrupted as `rate_limited`,
  attempt on Codex succeeded, task completed, two notifications;
- `just demo` runs the same loop against a real herdr in a throwaway session with
  `fake-agent` in the panes (a `live` test, not in CI);
- no real agent is started and no quota is spent.

**Status (2026-10-02): done.** `tests/integration/test_slice0.py` drives the loop with fakes;
`just demo` runs it against a real herdr in a throwaway session (about 10 s). Limits the slice
accepts on purpose, each picked up by a later step:

- A task is one prompt: the first finished turn completes it (step 3 decides what "done" is).
- A live attempt is not re-attached after batond restarts (step 6).
- If a turn ends without baton ever seeing the agent work, the runner waits for its timeout.
  Adapters reduce this by recognising work on screen (Codex footer spinner).
- Telegram only sends messages (step 4); `[scheduler] timezone` defaults to UTC and should be
  set to the local zone for reading printed reset times.

## MVP: usable every day on one machine

The first version a person can rely on unattended. It cuts across the steps below.

| Item | Status |
|------|--------|
| `baton daemon`: keeps running tasks, resumes waiting ones when a limit resets | ✅ (2026-10-03) |
| Resume interrupted attempts in their own session; restart when a session is gone | ✅ verified live with Claude Code |
| Re-attach to an active attempt after batond restarts; re-check attempts waiting for a person each cycle, without repeating the notice | ✅ verified live with Claude Code |
| "Done" for multi-step tasks: the `[[BATON:END ...]]` contract (agents.md design note) | ✅ verified live with Claude Code |
| Telegram actions: approve / deny, answer, status; owner and chat lock, bound callbacks (ADR 0009) | ✅ built; actions verified live with Claude Code through the CLI, Telegram itself not yet (needs a bot token) |
| Install and operate: README walkthrough, systemd user unit, `baton doctor` | ✅ units verified under systemd with Claude Code |

The MVP is complete (2026-10-03). Telegram itself has not been tried live yet: it needs a bot token.

## Deepening the slice

Each step replaces a simulated or minimal part with the real one. The slice's integration
test keeps passing throughout.

| Step | Makes real | Notes |
|------|------------|-------|
| 1. Real agents (in progress) | Adapters for Claude Code, Codex, OpenCode | Start-up blockers (trust, hooks, update prompts), resume commands, refining `blocked` into permission / question, positive idle for Codex, crash detection with `processes`. Tested against `fixtures/`; opt-in live runs in the sandbox repository. |
| 2. Quota accounting ✅ | Budget | Collection: `baton-detect usage` (Rust, ADR 0011) records every response's tokens and Codex's `rate_limits` into their own tables; Claude deduplicated by `message.id`, Codex cumulative counts turned into deltas across resumes; golden-tested against `fixtures/*/usage-sample.jsonl`. Remaining budget per agent (ADR 0012): Codex reported, Claude estimated against a configured or learned 5-hour cap; the scheduler moves agents short of budget behind the others and records each choice as `task.agent_chosen`; `baton budget` and Telegram `/budget`. Later: Claude's status-line `rate_limits` instead of the estimate. |
| 3. Policy engine (permissions ✅, ADR 0013) | Scheduler and recovery | Wait for reset vs. hand off, context-full handling, crash restart in a fresh pane, the resume caps of ADR 0005, what is handed over to the next agent. |
| 4. Telegram interaction | Telegram | Approve / deny buttons for permission prompts, free-text replies for questions, status commands. Security design first: chat lock, callbacks bound to a task and attempt, no free-form shell. |
| 5. Benchmark ✅ | `bench/` | baton's own loop against simulated agents on simulated time: limits, crashes, hangs, consecutive limits, waiting for a reset, and budget-aware choice on or off. `just bench`, reproducible per seed; results in `bench/results/`. |
| 6. Hardening and v1 | Everything | Restart safety (replay the log on start), `events_lost` reconciliation under load, documentation, first tagged release. |

### baton-detect in Rust (ADR 0011)

- ✅ Phase 1: the usage collector (`baton-detect usage`).
- ✅ Phase 2: the screen classifier (`baton-detect classify`), equal to the Python detector on
  1,949 parity cases; `[detector] engine = "shadow" | "rust"` in batond.
- ✅ Step 4 (2026-10-05): seven real tasks under `shadow`, no mismatch; the Python rules are
  removed and baton-detect is the only classifier. Answers pinned in
  `tests/golden/classify.jsonl`.

### Step 1 progress

- ✅ OpenCode adapter; all recorded captures of the three v1 agents are classified in tests.
- ✅ Safe start-up answers (only Codex's update prompt); trust, hooks and sign-in go to a person.
- ✅ Per-attempt decisions are a pure state machine (`scheduler.turn`).
- ✅ Live runs: Claude Code, Codex and OpenCode each completed a task driven by `baton run`
  (see `docs/research/agents.md`, "First live baton runs"); one state bug found and fixed.
- ✅ Interrupted attempts are resumed in their own session (`resume_command`, fresh pane):
  after a usage limit once the reset passes (when no other agent can take over), after a
  crash or stall up to `max_failure_resumes` times; a full context goes to a person.
- Not done, on purpose: a separate process-list crash check. herdr identifies the agent from
  its process, and every recorded crash showed `agent: null` as soon as the process died,
  which baton already treats as a crash. `PaneHost.processes` stays available for a case
  where that is not enough.
- Next: a live run of the resume path with a real agent (small quota), then step 2.

## After v1

- v1.1: Gemini CLI, once an authenticated round of captures exists and a session id can be
  obtained without herdr's help.
- Web panel and REST API, each with its own ADR (ADR 0008).
