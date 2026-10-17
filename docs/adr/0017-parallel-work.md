---
status: accepted
date: 2026-10-17
---

# Parallel work: tasks side by side on every agent, and a project's independent steps in worktrees

## Context and Problem Statement

batond runs one task at a time. `run_pending` awaits each task before it starts the next, so
while Claude works on a task, Codex sits idle with quota to spare, and a queue takes as long
as its tasks added up. The event log of a working week shows it: tasks waited up to 33
minutes in the queue for a turn of five, with a second agent free the whole time. A
project's steps run strictly one after another (ADR 0016), so a website of six steps takes
six turns even when four of them touch different files.

How can baton keep every agent it has busy, and several sessions of one agent when its plan
allows, without agents overwriting each other's work and without losing what the log
guarantees (ADR 0002)?

## Decision Drivers

- The result must feel faster to a person: wall-clock time to a finished queue or project.
- Two agents must never edit the same directory at the same time unless baton isolated them.
- Limits and budgets keep working: an agent short of budget still goes behind the others
  (ADR 0012), and a limit still hands a task on.
- Everything stays in the event log; a restart re-attaches every attempt that was running.
- Plans written before this change, and logs written by 1.x, mean what they meant.

## Considered Options

1. Several batond processes, one per agent.
2. One batond that drives several tasks concurrently, bounded by slots per agent.
3. Option 2, plus parallel steps in a project, each step in a git worktree of its own,
   merged back when it completes.

## Decision Outcome

Chosen option: **3**.

### Tasks side by side (scheduler)

- **Slots.** `[scheduler] agent_slots` sets how many tasks one agent runs at once (default 2
  per agent, e.g. `{claude = 3, codex = 2}`), and `max_parallel_tasks` (default 4) bounds
  them all. A slot is held by an attempt that is active and not waiting for a person: a
  task blocked on a question does not stop other work.
- **Dispatching.** Each cycle batond starts, as its own asyncio task, every ready task it is
  not already driving, oldest first, while a free slot exists on an agent that is
  available (not limited). A worker that ends wakes the loop at once, so a freed slot is
  filled without waiting for the idle time.
- **Choosing.** The agent choice (ADR 0012) puts agents without a free slot behind those
  with one, then orders by budget, then by preference. It never refuses an agent for being
  full: a hand-off after a limit may briefly put one task over the slots, which is better
  than a task that cannot move. Choosing and recording the attempt's start happen under one
  lock, so two tasks starting together see each other's slot.
- **One task per directory.** A task does not start while another task has a live attempt
  in the same directory (resolved path). Two agents in one checkout would overwrite each
  other; tasks for one repository still queue behind each other unless they are a
  project's parallel steps, which get their own directories (below).
- **Stopping.** Stopping batond cancels every worker. Their attempts stay active in the log
  and are re-attached on the next start, as a single run's was.

### A project's independent steps (projects)

- **The plan says what may run together.** A plan step may carry `needs`: the numbers of
  the steps it builds on (only earlier ones). A step without `needs` builds on the step
  before it, which keeps every existing plan sequential. The planning prompt asks for
  `needs` and for steps that can be done side by side to touch different files.
- **A sequential plan runs as before**, in the project directory.
- **A plan with independent steps** runs each step in its own git worktree,
  `<root>/.worktrees/<project dir>/step-<n>`, on branch `baton/step-<n>`. The worktree is
  opened from the project's branch once every step it needs has been merged (recorded as
  `step.worktree_opened`). Before the first worktree is opened, what is in the project
  directory is committed, so every step starts from the plan.
- **Merging.** When a step completes, baton commits what is in its worktree and merges its
  branch into the project directory (`git merge --no-ff`), one merge at a time, recorded as
  `step.merged`. A step that needs it starts only then.
- **Conflicts.** A merge that conflicts is aborted (`step.merge_conflicted`) and baton adds
  a merge task (`role = "merge"`, `after` the step) in the project directory: an agent
  merges the branch and resolves the conflicts, keeping both steps' work. When that task
  completes, a merge it left uncommitted is committed, unless a file still holds a conflict
  marker. If the branch is then part of the project's history, the step is merged; if not,
  the steps waiting for it are cancelled and the operator is told, as for a failed step.
  While a merge task is open, no other merge is made and no worktree is opened.
- **Delivery** waits until every step is merged.
- **Folder trust.** Worktrees are under `[projects] root`, so an agent's folder-trust
  question there is answered as in the project directory (ADR 0016).

### The event log

Additive to the 1.x types:
- `task.created` gains `needs` (task ids, default empty), `branch` (the step's worktree
  branch, default none), and `role = "merge"`.
- the plan's steps gain `needs` (default none: the step before).
- new notes about a step: `step.worktree_opened` (path, branch, base commit),
  `step.merged` (commit), `step.merge_conflicted` (files) and `step.merge_failed` (why: the
  merge task completed, the branch is still not merged). They may follow the step's
  completion.

### Consequences

- Good: a queue of independent tasks finishes about as fast as its longest task, up to the
  number of slots, instead of as slow as all of them together.
- Good: a project's independent steps run at the same time on Claude and Codex.
- Good: each step's work is a commit on its own branch, so what a step did can be read and
  undone with git.
- Bad: several agents at once spend the quota faster. A limit is reached sooner; it is
  handled as before (hand-off, waiting for the reset), and `agent_slots = {claude = 1}`
  brings the old pace back.
- Bad: merge conflicts cost a merge task. The planning prompt keeps them rare by asking for
  steps that touch different files.
- Bad: a machine runs several agent processes at once; each holds hundreds of MB.
- Bad: tasks outside a project for the same directory still run one after another.

## Pros and Cons of the Options

### 1. One batond per agent

- Good: no change to the loop.
- Bad: processes would compete for the same tasks; the log has no lease, and handing a task
  over between processes would need one.

### 2. Concurrent tasks only

- Good: small; independent tasks run side by side.
- Bad: a project, the main way work arrives from Telegram, stays sequential.

### 3. Concurrent tasks and parallel steps in worktrees

- Good: both kinds of work get faster.
- Bad: baton writes to the project's repository (commits and merges) for the first time,
  only in directories it made for a project.
