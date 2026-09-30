---
status: accepted
date: 2026-09-30
---

# Scope of v1

## Context and Problem Statement

The original plan listed four agents, a REST API, a web panel, a Telegram bot and a Rust
detector. After H0 there is enough evidence to cut this down to what is needed for coban's
core promise: a task is carried to completion across usage limits and crashes, on whichever
agent has quota, and the user is told when a decision is needed.

What is in v1, and what is explicitly not?

## Decision Drivers

- The shortest path to an end-to-end loop that a person can use daily.
- Only build on agents for which H0 produced real data.
- Fewer moving parts to secure: an unattended process that can type into terminals should
  not also expose an HTTP API by default.

## Decision Outcome

**In v1**

- Agents: **Claude Code, Codex CLI, OpenCode**.
- One process, `cobanD`: event log (SQLite), herdr socket client, adapters, Python detector
  (ADR 0007), budget, scheduler, recovery.
- Interfaces: the `coban` CLI and a **Telegram bot running inside the cobanD process**
  (aiogram, long polling: no inbound port).
- Configuration by file and environment, as today.

**Not in v1**

- **Web panel** and **REST API**. `web/` and `coban.api` stay as empty placeholders.
- **Gemini CLI**, deferred to v1.1: H0 has only its first-run screens, herdr has no Gemini
  integration and therefore no session id, and only two detection rules exist for it. The
  targeting rule of ADR 0006 would be permanently in its weak mode.
- A Rust detector (ADR 0007).
- Multiple machines, multiple users, remote herdr servers.

**Dependencies removed from the plan**: FastAPI and an ASGI server; React, Vite and
TypeScript; tokio, serde, notify and regex. Dependencies still planned, added with the
module that uses them: aiogram.

`AgentKind.GEMINI` and the Gemini fixtures stay in the repository: they are data, and the
adapter slot is kept for v1.1.

### Consequences

- Good: v1 is one Python process with one outbound connection (Telegram) and one local
  socket (herdr).
- Good: every supported agent has recorded screens, events and a usage sample.
- Bad: no dashboard; state is visible through the CLI and Telegram only.
- Bad: Gemini users wait for v1.1.
- A REST API or web panel added later needs its own ADR, including how it is authenticated.

## Considered Options

1. Everything in the original plan.
2. The reduced scope above.
3. CLI only, no Telegram.

Option 1 spreads effort across parts that do not affect whether a task finishes. Option 3
drops the one channel that reaches the user when an agent is blocked and nobody is at the
terminal, which is the situation coban exists for.
