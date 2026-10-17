# 2026-10-17: parallel work

## Why

The event log of a working week showed batond's queue was the bottleneck, not the agents:
tasks waited up to 33 minutes for a five-minute turn while Codex sat idle with quota to
spare, because `run_pending` awaited each task before starting the next. Projects ran their
steps strictly in order.

## What changed (ADR 0017)

- `scheduler/dispatch.py`: one asyncio worker per task. `queue.dispatch` starts tasks while
  an available agent has a free slot (`[scheduler] agent_slots`, two per agent;
  `max_parallel_tasks`, four), never two in one directory. The runner prefers agents with a
  free slot and holds a lock from choosing an agent to recording the attempt. A worker that
  ends wakes the loop.
- Projects: a plan step may carry `needs`. A plan with independent steps puts each step in
  a git worktree (`<root>/.worktrees/<project>/step-<n>`, branch `baton/step-<n>`);
  `projects.flow.advance_steps` opens a worktree once what the step needs is merged, and
  merges completed steps back one at a time (`workdir/repo.py`, the first code that writes
  to a repository). A conflict becomes a merge task; a merge task that leaves the conflict
  stops the project.
- New notes: `step.worktree_opened`, `step.merged`, `step.merge_conflicted`,
  `step.merge_failed`; `task.created` gains `needs`, `branch` and `role = "merge"`.

## Results

`just bench`, 30 worlds of a ten-task queue (`bench/results/2026-10-17.md`):

| Mode | Queue finished in (median) | Limits hit | Tokens |
|---|---|---:|---:|
| one at a time (1.1) | 2.36 h | 30 | 25.52M |
| one session per agent | 68 min | 52 | +1.4 % |
| default, two per agent | 46.7 min | 56 | +3.4 % |
| three per agent | 35.4 min | 72 | +4.7 % |

More sessions hit limits more often: the budget reserve (ADR 0012) is checked when an agent
is chosen and does not count the other sessions of that agent drawing on the same window.

## Not done

- A live run of a parallel project with real agents.
- Budget estimates that share one window among concurrent sessions.
- `baton run` does not advance projects (it never did: plans are proposed by batond), so a
  parallel project's worktrees open only under batond.
