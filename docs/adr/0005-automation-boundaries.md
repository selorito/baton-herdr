---
status: accepted
date: 2026-09-30
---

# Boundaries for automating vendor agents

## Context and Problem Statement

coban drives coding agents that users run under their own subscriptions or API keys. The
vendors' terms (summarised with sources in `docs/research/agents.md`) restrict how those
accounts may be used by third-party software: Anthropic does not allow third parties to
collect or intermediate Claude credentials and states that plan limits "assume ordinary,
individual usage"; Google states that accessing the services behind Gemini CLI with
third-party software through its OAuth is a violation. coban's purpose, resuming work
automatically, sits close to these lines.

What may coban do, as a matter of design, regardless of what is technically possible?

## Decision Drivers

- Users must not lose their accounts because they used coban.
- The rules must be enforceable in code review, not left to judgement per feature.

## Considered Options

1. No constraints: do whatever the agent CLI allows.
2. Drive only official, unmodified agent binaries through their terminal interface, never
   touch credentials, and rate-limit automation.
3. Support only API-key (usage-billed) accounts.

## Decision Outcome

Chosen option: **2**, as hard rules:

1. coban starts and drives only the vendor's unmodified CLI binary, in a terminal pane, the
   way a person would. It never calls a vendor's private or subscription API itself.
2. coban never reads, copies, stores, logs or transmits agent credentials or session tokens
   (for example `~/.claude`, `~/.codex/auth.json`, Gemini OAuth files). Usage accounting reads
   only session and usage logs.
3. Sign-in is always done by the user in the agent's own flow. coban stops and asks.
4. Automatic resume is bounded: coban waits for the reported reset time, resumes at most one
   attempt per agent account at a time by default, and applies a configurable cap on
   automatic resumes per task and per day. When the cap is hit, a human decides.
5. coban does not try to evade limits: no rotating accounts, no retry storms, no disguising
   automation.
6. The README states that users are responsible for their vendor's terms, and links them.

This is an engineering constraint, not legal advice. A change to any rule needs a new ADR.

### Consequences

- Good: a clear line for contributors; features that need credentials are out of scope.
- Good: behaviour stays close to one person working through a queue of tasks.
- Bad: coban cannot use richer private APIs (for example account-level quota endpoints) even
  where they would be more reliable than screens and logs.
- Bad: the caps make fully unattended operation slower than it could be.

## Pros and Cons of the Options

### No constraints

- Good: maximum capability.
- Bad: exposes users to suspension and the project to takedown requests.

### API-key accounts only

- Good: usage-billed automation is the vendors' intended path.
- Bad: excludes the subscription plans that most individual users have, which is the main
  use case (quota-aware scheduling only matters when there is a quota).
