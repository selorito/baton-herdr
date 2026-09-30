---
status: accepted
date: 2026-09-30
---

# The detector is written in Python first; Rust only if a benchmark justifies it

Supersedes the language choice for the detector in
[ADR 0001](0001-hybrid-rust-python.md). The rest of ADR 0001 (the detector sits behind an
NDJSON contract defined by JSON Schema, separate from orchestration logic) still holds.

## Context and Problem Statement

ADR 0001 chose Rust for `coban-detect` on the assumption that screen classification runs
per snapshot × pane and must stay cheap. H0 changed the picture:

- herdr already does the hot path. It parses PTY output, keeps the detection snapshot, and
  pushes `pane.agent_status_changed`. coban does not watch bytes; it reacts to events and
  reads a small text snapshot when one arrives.
- What coban must add is a handful of string and regex checks per agent (limit messages,
  context-full lines, start-up dialogs, resume pickers, a positive idle signal for Codex) on
  a few dozen lines of text, a few times per minute per pane.
- The rules will change often: agents update themselves silently and their wording moves
  (H0 saw herdr's own Codex rules go stale within one release). The rules are agent-specific,
  so by the module rules they live in `adapters/`, which is Python.
- A second toolchain in the critical path slows every change while the product is still being
  discovered.

Which language should the detector be written in now?

## Decision Drivers

- Speed of changing detection rules, tested against `fixtures/`.
- One place for agent-specific knowledge (`adapters/`).
- Keep the option to move to Rust without touching callers.

## Considered Options

1. Rust now, as ADR 0001 planned.
2. Python now, behind the same NDJSON contract; Rust later only if measured cost demands it.
3. Python with no contract (plain function calls only).

## Decision Outcome

Chosen option: **2**.

- The detection engine and rule types are pure Python. Agent rule sets live in `adapters/`.
- The NDJSON contract from ADR 0001 is kept as the detector's external interface: one JSON
  object in (agent, agent version, screen text, herdr status and explain evidence), one out
  (coban `AgentState`, evidence, optional reset time), described by JSON Schema in the
  repository. In-process callers use the same request and response models.
- `bench/` gets a benchmark that replays the recorded fixtures at realistic and at stressed
  rates (number of panes × events per second). Rust is reconsidered only if that benchmark
  shows detection taking a meaningful share of a core at the pane counts coban targets. Any
  such change needs a new ADR with the numbers.
- `crates/coban-detect` stays as the stub it is (a `--version` binary with its test) so the
  workspace and CI job do not rot; no Rust feature work is planned. The planned Rust
  dependencies (tokio, serde, notify, regex) are dropped from the plan.

### Consequences

- Good: detection rules, their tests and the fixtures are in one language and one test run.
- Good: nothing to build or ship besides the Python package for v1.
- Good: the contract keeps a later Rust implementation a drop-in replacement.
- Bad: if pane counts grow far beyond the target, Python detection may need to be replaced;
  the benchmark exists to see that coming.
- Bad: the repository carries a Rust stub that does nothing yet.

## Pros and Cons of the Options

### Rust now

- Good: best performance.
- Bad: optimises a path herdr already covers; slows rule changes; splits agent knowledge
  across two languages.

### Python with no contract

- Good: least code.
- Bad: gives up the cheap exit to Rust and the ability to test the detector from outside
  with recorded input and output.
