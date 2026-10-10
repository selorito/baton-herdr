---
status: accepted
date: 2026-10-10
---

# Projects from Telegram: a request is planned, approved, run step by step, and delivered

## Context and Problem Statement

baton runs tasks someone added at the terminal (`baton task add`), one at a time, each on
one agent, handing it to another when a limit or a failure stops it. Telegram only reports
and answers what baton asks.

The goal the project started from is larger: someone writes "build me a website" in
Telegram, away from the machine, and gets a website back. That needs four things baton
does not do: take work from Telegram, turn a request into steps, run the steps, and show
the result. How should a request become delivered work, without giving Telegram a shell?

## Decision Drivers

- The operator decides: nothing runs on a request until the operator has seen the plan.
- Telegram never names a directory, a command or an agent. A request is text that becomes
  an agent's prompt, in a directory baton creates.
- Reuse the task engine: every step is an ordinary task, so limits, hand-offs, stalls,
  permissions and questions work as they do now.
- Every step of the way in the event log (ADR 0002), as additive changes to the 1.x event
  types.
- Small first: steps run one after another. Running them in parallel is a later decision.

## Considered Options

1. One task per request: the whole request is one prompt to one agent.
2. A plan, approved by the operator, whose steps run as tasks in order.
3. A plan whose steps run in parallel on several agents, each in its own worktree.

## Decision Outcome

Chosen option: **2**, built so that 3 can follow.

**A request.** `/new <request>` in Telegram (or `baton project new "<request>"`) creates a
project:
- a new directory `<root>/<slug>-<id>`, with `root` from `[projects] root` (default
  `~/dev/baton-projects`);
- a git repository there with one empty commit, so what the project changed can be
  measured;
- a **plan task** (`task.created` with `project` and `role = "plan"`).

The planning agent is asked not to write code. It writes the plan to `.baton/plan.json`:
a summary and at most eight steps, each a title and instructions. When the plan task
finishes, baton reads and checks the file:
- a valid plan is recorded as `plan.proposed`, and the operator gets the steps in Telegram
  with **Start** and **Cancel**;
- a missing or invalid plan comes to the operator as a failed plan, and nothing runs.

**Approval.** **Start** records `plan.approved` and creates one **step task** per step
(`role = "step"`), each `after` the one before it. **Cancel** records `plan.rejected`.

**Running.** A task with `after` waits until that task has completed. A step is an ordinary
task: its prompt is the step's instructions, with the request and the plan around them so
the agent knows where the step fits. If a step fails or is cancelled, the steps after it
wait, and the operator is told.

**Delivery.** When the last step completes, baton records `project.delivered` and sends:
- a summary: the steps, and the files the project changed since its first commit;
- if the project has a web page (`index.html` at its root, or in `dist/`, `build/`,
  `public/` or `site/`), a link to it and a screenshot of it.

The link is a read-only static server batond runs over the projects root, on
`[projects] preview_host` and `preview_port`. The screenshot is taken with a headless
Chromium (`[projects] browser`). Without a browser there is no screenshot; without a page
there is neither.

**The event log.** `task.created` gains four optional fields: `project`, `role`, `after`, and
on a plan task the operator's `request`.
New events: `plan.proposed`, `plan.approved`, `plan.rejected`, `project.delivered`. They
are notes about a task, so they may follow its completion. Logs written by 1.0 read as
before.

### Consequences

- Good: a request typed on a phone ends as a project on disk, a link and a picture, with
  the operator choosing when work starts.
- Good: each step has the recovery of an ordinary task; a limit mid-project moves that step
  to another agent.
- Good: Telegram still has no shell. Its text reaches an agent only as a prompt, in a
  directory baton made, under the same permission policy (ADR 0013).
- Bad: steps run one after another, so a project takes as long as its steps added up.
- Bad: the plan is only as good as the planning agent. Eight steps at most keeps it
  reviewable on a phone, not necessarily right.
- Bad: the preview server exposes the projects root, read-only, to whoever can reach
  `preview_host`. The default is `127.0.0.1`, which a phone cannot reach. Opening it to the
  network is the operator's choice.
- Bad: agents ask before running tests and builds in a new project unless the projects
  root is in the policy's `trusted_dirs`; `baton doctor` says so.

## Pros and Cons of the Options

### 1. One task per request

- Good: no new concepts.
- Bad: a large request is one long turn on one agent, with nothing to review before it
  runs and no sense of progress.

### 2. A plan, approved, run in order

- Good: the operator sees and stops the work before it runs; progress per step.
- Bad: no parallelism.

### 3. Parallel steps in worktrees

- Good: faster on independent steps.
- Bad: needs merging and conflict handling, and runs several agents at once against
  their limits. Worth doing once 2 works.
