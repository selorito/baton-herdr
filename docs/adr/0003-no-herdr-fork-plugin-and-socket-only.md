---
status: accepted
date: 2026-09-24
---

# No herdr fork: integrate through the socket API and plugins only

## Context and Problem Statement

coban depends on herdr to run agents in terminal panes, read their screens, send input, and
receive events. herdr is actively developed, with a stable and a preview release channel, and
it treats its public endpoint contract as a compatibility promise.

How should coban integrate with herdr?

## Decision Drivers

- Keep up with herdr releases without merge work.
- Users should be able to run coban against the herdr they already have installed.
- Keep the herdr-specific surface small so it can be faked in tests.

## Considered Options

1. Fork herdr and add the orchestration features inside it.
2. Contribute orchestration features to herdr upstream.
3. Use herdr as-is: its socket API / CLI for control and state, and its plugin system for
   pushing events to cobanD.

## Decision Outcome

Chosen option: **3**. coban never forks or patches herdr. It talks to herdr only through the
documented socket API and CLI, and ships a herdr plugin (`herdr-plugin/`) that forwards events.
Only the `herdr` Python package knows this protocol. Everything else depends on a
`typing.Protocol` in `core`, so tests use a fake herdr.

### Consequences

- Good: no fork to maintain; coban works with a stock herdr install.
- Good: a narrow, fakeable boundary.
- Good: herdr's own compatibility guarantees for its public API protect coban.
- Bad: coban is limited to what the API and plugins expose. If something is missing, the
  options are a workaround or an upstream feature request through herdr's normal process.
- Bad: coban must track herdr's API changes and declare which herdr versions it supports.

## Pros and Cons of the Options

### Fork herdr

- Good: full control, any feature possible.
- Bad: permanent rebase cost against a fast-moving upstream; users must replace their herdr.

### Contribute upstream

- Good: no extra process.
- Bad: orchestration policy is out of herdr's scope, and herdr only accepts implementation
  work through its maintainer-controlled process. coban cannot depend on that.
