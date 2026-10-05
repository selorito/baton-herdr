# Changelog

All notable changes to this project are documented here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and the project uses
[Semantic Versioning](https://semver.org/spec/v2.0.0.html). For 1.x, the public interface
is:
- the `baton` CLI;
- the configuration files (`baton.toml`, `policy.yaml`);
- the event types in the log;
- the JSON contracts in `schemas/`.

## [Unreleased]

## [1.0.0] - Unreleased

The first release: baton runs a queue of coding tasks unattended on one Linux machine,
across Claude Code, Codex CLI and OpenCode in herdr.

### Upgrading from 0.1: behaviour changes

- **batond now answers some permission prompts itself.** Read-only shell commands
  (`ls`, `cat`, `grep`, `git status`, `git diff`, …) that Claude Code asks to run are
  approved without asking you. Tests and builds are approved only in the `trusted_dirs`
  of `~/.config/baton/policy.yaml`; everything else still comes to you. To keep 0.1's
  behaviour (every prompt to you), set `[policy] enabled = false`. `baton doctor` warns
  while no `policy.yaml` exists and states what the defaults approve (ADR 0013).
- **A hang is caught after 15 minutes** of a still screen and no recorded tokens, not
  after the one-hour turn timeout (ADR 0014). Tune with `[scheduler] stall_minutes`.
- **baton-detect is required.** 0.1's `[detector] engine = "python"` (the default) and
  `"shadow"` still load but run as `"rust"`; remove the setting (`baton doctor` warns).
  While baton-detect gives no answer, batond finishes and hands off nothing.

### Added

- **Tasks and the event log.** `baton task add`, `baton status`. Every state change is an
  immutable event in an append-only SQLite log, and the board is folded from it
  (ADR 0002).
- **batond** (`baton daemon`, or systemd user units from `baton service install`):
  - runs tasks one at a time;
  - re-attaches to active attempts after its own restart;
  - wakes when a limit resets.
- **Agents in herdr**:
  - adapters for Claude Code 2.1, Codex CLI 0.156 and OpenCode 1.18;
  - screen detection of usage limits (with their reset time), full contexts, permission
    prompts, questions and crashes, tested against recorded screens in `fixtures/`;
  - safe start-up answers (only Codex's update prompt).
- **Stall detection** (ADR 0014): a working agent whose screen and recorded tokens both
  stay still for `stall_minutes` (15; 30 without usage collection) is resumed. The reason
  is recorded on `attempt.interrupted` as `detail`.
- **Recovery** (ADR 0005):
  - a usage limit hands the task to the next available agent, or waits for the reset
    and resumes the same session;
  - crashes and stalls resume the agent's own session, up to `max_failure_resumes`;
  - a session that cannot be reopened restarts fresh.
- **CLI commands wake batond at once** (`task add`, `approve`, `deny`, `answer`), through
  a FIFO next to the event log, instead of waiting for its next cycle (up to 30 s).
- **End-of-turn contract.** `[[BATON:END status=done|question|blocked]]`, so a question
  is never taken for a finished task.
- **Operator actions** from Telegram and the CLI: approve, deny, answer. Each is bound to
  the prompt it was shown for, carried out at most once, into a verified pane (ADR 0009).
- **Telegram notices** in HTML:
  - the chosen agent and why;
  - hand-offs, with reset times in local time;
  - the end of the screen when a person is needed;
  - duration, tokens, changed files and budget left when done.

  Secrets are masked before sending. `/status` and `/budget`.
- **Usage collection** with `baton-detect usage` (Rust). It reads tokens per response from
  the agents' own logs, and Codex's rate-limit windows (ADR 0011).
- **Remaining budget per agent**: Codex as reported, Claude estimated against a
  configured or learned five-hour cap. The scheduler moves short agents behind the
  others and logs each choice with its reason. `baton budget` (ADR 0012).
- **Permission policy**: `~/.config/baton/policy.yaml` allows, asks about or denies agents'
  shell commands by rule. Defaults: read-only commands are allowed; tests and builds only
  in `trusted_dirs`; risky commands ask. Each decision is logged. `baton policy check`
  (ADR 0013).
- **Screen classifier in Rust** (`baton-detect classify`), the only one batond uses. It
  replaced the Python rules after matching them on 1,949 cases and on real tasks in
  `shadow` mode (ADR 0011). Its answers are pinned in `tests/golden/classify.jsonl`.
  `classify --rules DIR` tries changed rules without a rebuild.
- **`baton doctor`**: config, database, herdr and its integrations, agents, baton-detect,
  detector, policy, time zone, Telegram.
- **Benchmark**: `just bench` runs baton's own loop against simulated agents on simulated
  time; the same seed gives the same report.
- **Releases**: prebuilt `baton-detect` for Linux x86_64 and aarch64, and the Python wheel.

### Security

- Rule patterns that need look-around run with a backtracking limit; all others run on a
  linear-time regex engine. A hostile screen cannot stall the classifier.

[Unreleased]: https://github.com/selorito/baton-herdr/compare/v1.0.0...HEAD
[1.0.0]: https://github.com/selorito/baton-herdr/releases/tag/v1.0.0
