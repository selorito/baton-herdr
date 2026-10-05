---
status: accepted
date: 2026-10-03
---

# baton-detect is written in Rust, for reasons other than speed

Partially supersedes [ADR 0007](0007-detector-in-python-first.md): its decision that the
detector stays in Python, and that Rust waits for a benchmark. ADR 0007's NDJSON contract and
its tests against `fixtures/` still hold.

## Context and Problem Statement

ADR 0007 kept the detector in Python because the only argument for Rust was performance, and
herdr already does the hot path. That is still true. But there are other reasons for a
separate Rust binary that ADR 0007 did not weigh, and roadmap step 2 (quota accounting) adds
a second detector job: reading the agents' usage logs as they grow.

Where should `baton-detect` live, and how does it replace the Python detector without a
risky switch-over?

## Decision Drivers

- (a) **One long-running binary, independent of the Python service.** Following log files and
  a SQLite database is a process of its own; it should keep its own state, restart on its own
  and be usable without batond (for example from `baton doctor` or by hand).
- (b) **Alignment with herdr.** herdr's agent-detection rules are TOML with a matching engine
  in Rust (Apache-2.0). A Rust detector can use the same format and matching logic, so rules
  baton needs can later be offered upstream. Code taken from herdr keeps its license header
  and is credited in a NOTICE file.
- (c) **Type safety in parsers.** Usage logs and screen rules are untrusted, versioned formats
  that change without notice; serde types make every assumption explicit and every deviation
  an error that is reported, not a silent wrong number.
- Changing the detector must never put task supervision at risk.

## Decision Outcome

`baton-detect` is implemented in Rust (`crates/baton-detect`).

**Migration: strangler fig.**

1. Rust implements the same NDJSON contracts (`schemas/`).
2. Python and Rust run over the same fixtures and their outputs are compared (parity tests).
3. Once they match, a setting (`[detector] engine = "python" | "shadow" | "rust"`)
   switches batond to Rust; `shadow` first runs both and logs every difference.
4. Then the Python detector is removed.

**Phase 1: the usage collector.** `baton-detect usage` follows Claude Code and Codex JSONL
logs and OpenCode's SQLite database and writes one NDJSON line per usage record and per Codex
rate-limit observation (`schemas/usage-event.v1.json`). There is no Python usage reader, so
its parity test checks the binary's output against the schema and the golden files instead.
The screen classifier is phase 2.

**Phase 2: the screen classifier.** `baton-detect classify` reads one `DetectionRequest` per
line and writes one `DetectionResult` (`schemas/detector-*.v1.json`), the same contract as
`baton detect`.

- **Rules are data.** `crates/baton-detect/rules/<agent>.toml` is compiled into the binary.
  Each rule is one pattern over one region. The field names follow herdr's manifests (`id`,
  `state`, `region`, `regex`), and herdr rule ids still map to blocked kinds.
- **The region** is `bottom_non_empty_trimmed(N)`: the last N non-blank lines,
  right-trimmed. That is the Python detector's view. It is deliberately not called herdr's
  `bottom_non_empty_lines`, which keeps blank lines.
- **Patterns are the Python ones, verbatim.** The only change is `\Z`, which Rust writes `\z`. Lines are split and trimmed
  the way Python does it, and local times resolve the way Python's `zoneinfo` resolves them
  (`jiff`).
- **Two engines, bounded.** A screen is text an agent printed, so it can be shaped by
  whatever the agent reads, and a pattern that backtracks badly on it would stall
  batond. A pattern runs on `regex`, which matches in linear time, whenever it compiles
  there. Only the two that need a look-ahead (`codex_idle_prompt`,
  `opencode_idle_composer`) run on `fancy-regex`. They run with a limit of 100,000
  backtracking steps per screen (`rules::BACKTRACK_LIMIT`). A pattern that reaches the
  limit counts as no match: the screen is read again a moment later, and no answer is
  better than a hung classifier.
  - Neither look-ahead rule is exponential today: `fancy-regex` hands the parts without
    look-around to the linear engine, and the opencode rule reads at most 12 lines. The
    limit guards the next rule someone writes. Tests show a hostile screen (lines of
    800,000 characters) answered in under a second, and an exponential pattern giving up.
  - The limit is far above what a terminal screen needs, and the parity run shows no
    answer changing. A region of more than ~100,000 characters could make
    `opencode_idle_composer` give up where Python would answer; herdr screens are a few
    thousand characters.
  - The Python detector had no such limit. It went in step 4.
- **Parity** (`tests/integration/test_detect_parity_binary.py`): every recorded capture
  under every host state, limit messages at moments around daylight-saving changes in seven
  zones, the fake agents' screens and edge cases go through the binary in one run. In
  total, 1,949 cases. Each answer must equal Python's. The corpus is built from the current
  fixtures and rules when the test runs, so nothing generated is committed.
- **In batond**, `[detector] engine` picks the detector:
  - `python` (the default): the adapters' rules.
  - `shadow`: Python decides, the Rust classifier answers every screen too, and each
    difference is logged as "detector mismatch" (states and evidence, never the screen).
  - `rust`: Rust decides, and Python answers while the process is unavailable.

  The process is one long-running child. A crash, a late answer or an unreadable answer
  restarts it, and it is not retried for a minute.
- **Step 4** removed the Python rules once `shadow` had run on real work without a mismatch.

**Step 4, done (2026-10-05).**

- **The evidence.** Seven small tasks ran in a sandbox repository under `shadow`, with
  Claude Code 2.1 and Codex CLI 0.160. Every screen batond read went through both
  classifiers, about 13 minutes of supervision, and none differed. The same week, the
  parity test had matched on 1,949 cases.
- **What changed.** `baton-detect classify` is the only classifier. The Python rules,
  their engine (`ScreenRule`, `detect`) and their clock parsing are gone, as are the
  `python` and `shadow` modes.
  - `[detector] engine` still accepts those values, so an older config loads. They run
    as `rust`, and `baton doctor` asks for the setting to be removed.
- **While the process gives no answer**, batond uses `HostDetector`:
  - herdr's working and blocked pass through;
  - herdr's idle reads as unknown, because only the rules can tell a finished turn from
    a usage limit or a question.

  Without the classifier, no task finishes, hands off or starts; it waits, and its
  timeouts bring in a person. Before step 4, Python answered instead.
- **How the rules stay tested.**
  - The parity test became a golden test. The 1,949 answers, as both classifiers gave
    them, are pinned in `tests/golden/classify.jsonl` and checked against the binary.
  - A deliberate rule change regenerates them (`UPDATE_GOLDEN=1`) and shows up in review.
  - The mutation test moved to the Rust rules. `classify --rules DIR` loads rules from
    files, so each rule is deleted or loosened in turn without a rebuild, and some
    answer must change.
  - The Python tests that classified screens now ask the binary. CI's Python job builds
    it and fails without it.

**Usage data is kept apart from the task event log.** Task events change state: projections
fold them into the board, and every one of them belongs to a task. Usage records are
telemetry: they belong to an agent session, arrive in volume, and are only ever summed. They
go into their own append-only tables (`usage_records`, `rate_limit_observations`), where a
`UNIQUE` record id lets the database drop duplicates (`INSERT … ON CONFLICT DO NOTHING`). The
task event model and its invariants do not change.

### Consequences

- Good: usage collection runs and restarts on its own; a crash in it does not touch task
  supervision.
- Good: a path to sharing detection rules with herdr.
- Bad: two toolchains in the product, not only in the repository; installing baton now also
  means building `baton-detect` (`cargo install`).
- Bad (until step 4): during the migration, detection logic existed twice; the parity tests kept the copies
  from drifting.
