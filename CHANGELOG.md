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

### Fixed

- **The usage collector no longer reads its own reads.** `baton-detect usage` took the
  events Linux raises when a file is only opened or read as changes, so every read
  caused another: about 200,000 reads a second and a CPU and a half busy for as long as
  batond ran, slowing the agents on the same machine. It now reads a log only when it
  was written to.
- `baton run` kept the result of a task that ended just as the run finished.
- **An agent that exits as it starts** is noticed within seconds (only a shell is left in
  its pane) instead of after the start timeout, and the task moves to another agent, or
  starts afresh, up to `max_failure_resumes` times; it used to wait for a person. Seen
  live: two Codex sessions started in the same second both exited on Codex's shared app
  server. Sessions of one agent now start one after another.
- **A signed-out agent is set aside.** Codex's sign-in screen (seen live after its refresh
  token had been used twice; `codex login status` still said logged in) used to look like
  a start that never finished. baton-detect now recognises it; the attempt is interrupted
  as `signed_out`, the task moves to another agent, the agent is not used for the limit
  cooldown, and the notice says what to run (`codex logout && codex login`).

### Added

- **Tasks run side by side** (ADR 0017). batond drives several tasks at once, each in its
  own worker: two sessions per agent and four tasks in all by default
  (`[scheduler] agent_slots`, `max_parallel_tasks`). An agent with a free slot is chosen
  before a busy one; a freed slot is filled at once. Two tasks never run in one directory.
  In the benchmark a queue of ten tasks finishes about three times sooner.
- **A project's independent steps run side by side** (ADR 0017). The planning agent marks
  what each step builds on (`needs`); steps that do not build on each other run at the same
  time, each in a git worktree of its own under `<root>/.worktrees/`, and are merged into
  the project directory as they complete. A merge that conflicts becomes a merge task for an
  agent. The plan notice shows which steps start at once. Plans without `needs` run in
  order, as before.
- **Lean agent sessions** (`[scheduler] lean_sessions`, on by default). The Claude sessions
  baton starts leave out the skills synced from claude.ai, Claude Code's bundled skills and
  the claude.ai connectors: each call reads 25.0k tokens instead of 30.7k, and a new
  session writes 2.4k to the cache instead of 8.1k (measured with Claude Code 2.1.296).
- **A model per kind of task** (`[scheduler.models]`): e.g. a project's plan on the default
  model and its steps on a faster one, `claude = { step = "sonnet" }`. Applied when a
  session starts and when it is resumed.

### Changed

- **A project's steps check their work without screenshots**, by reading the files and
  running them (`--dump-dom` for a page). In two earlier projects agents had read 41
  screenshots, each read again on every later turn; allowed two in the last step, an
  agent wrote them outside the project (a snap browser's directory) and waited for a
  person to allow reading them. baton takes the finished page's screenshot itself.

## [1.1.0] - 2026-10-12

For running baton unattended, and for labs and teams where each person runs their own.

### Added

- **`baton setup`** takes a fresh machine to a running batond in one guided pass:
  baton-detect, herdr's integrations, the config, Telegram (it finds your chat once you
  write to the bot), the policy and the service. It keeps what exists, so it can be run
  again. A Turkish guide for labs and teams: `docs/kurulum.md`.
- **Cancel from the phone.** `/cancel t-…` drops a task and closes its agent's pane;
  `/cancel p-…` drops a whole project. A notice with nothing to press or reply to carries
  a **Cancel task** button. `baton task cancel` and `baton project cancel` do the same.
- **A project whose step did not complete stops** instead of waiting for ever, and says so.
- **Folder-trust questions** are answered in a project's own directory and in the policy's
  trusted directories, so a first task in a trusted repository does not wait for you.
- **Projects from Telegram** (ADR 0016). `/new <request>` makes a project directory with a
  git repository and asks an agent to plan the request. The plan comes back with **Start**
  and **Cancel**; Start runs its steps one after another, as ordinary tasks. When the last
  step is done, baton sends what the project changed, a link to its page on a read-only
  preview, and a screenshot of it. From the terminal: `baton project new|start|cancel`.
  New settings: `[projects]`. New events: `plan.proposed`, `plan.approved`,
  `plan.rejected`, `project.delivered`; `task.created` gains `project`, `role`, `after`
  and `request`.
- **`baton task cancel <task>`** drops a task you no longer want.

- **Claude's own rate limits.** The Claude sessions baton starts get a status line,
  `baton-detect statusline`, which shows `5h 23% · 7d 54%` and records Claude's five-hour
  and weekly windows. `baton budget`, Telegram `/budget` and agent choice then use those
  figures instead of the estimate. Your own Claude sessions and settings are not changed
  (ADR 0015). New setting: `[usage] claude_status_log`. `baton doctor` shows Claude's last
  report and how old it is.

### Fixed

- **A Codex approval for a long command had no buttons.** The command pushed the
  approval's title above the lines baton reads, so it came as "❗ Needs you". Its choices
  now make it an approval whatever the command's length.
- **Finished agents stayed open.** Each finished or handed-off attempt left its agent idle
  in its pane, a few hundred MB each, and an agent idle after its limit held the session
  baton later resumed elsewhere. Their panes are now closed once the attempt is over
  (`[scheduler] close_finished_panes`, on by default); the conversations stay in the
  agents' own logs.
- **After herdr restarted, agents ran twice.** herdr resumes every pane's agent when it
  restores a session, so baton's resume of the same session failed and started afresh,
  with idle copies of old agents left running. `baton service install` now runs baton's
  herdr server with its own settings file (`~/.config/baton/herdr-server.toml`), in which
  herdr does not resume agents; baton resumes those of its tasks. Run
  `baton service install --force` once to get it.
- **A task cut short was reported done.** When herdr restarted, it reopened the agent's
  session idle in the middle of a turn, and batond took the idle agent for finished:
  ✅ came, the work was not there. A turn that ends without the end mark is now never
  taken for done; batond asks the agent to finish, twice at most, then asks you.
- **The event log could be broken by a race.** A task cancelled while batond watched it
  could get batond's next events after its cancellation, and batond could then no longer
  start. Appends that do not fit the log are now refused whole.
- **Stopping batond waited for the turn in progress**, which can take hours; it now stops
  at once and re-attaches at the next start. One task's fault no longer stops the loop.
- **Claude's tool permissions other than Bash's** (reading outside the working
  directories, for example) reached Telegram without buttons, and their first choice
  changes Claude's settings for good. They now come with Approve and Deny; Approve picks
  the one-time "Yes" on the screen as it is when pressed, or is refused without one.
- **A project's next step** started up to 30 s after the one before it finished; it now
  starts at once.
- **A task no longer lands in Codex's update chooser.** herdr's detection catalog reads
  Codex as idle from its window title, even right after launch and while the chooser is
  open; baton typed the task there and Enter picked "Update now". baton now waits until
  Codex's composer is on screen.
- **The Telegram bot is started again after it fails.** When batond started before the
  network (at boot), the bot stopped for good: notices still went out, but approvals and
  replies did nothing until batond restarted. A failed bot also made batond exit with an
  error when it stopped.

## [1.0.1] - 2026-10-06

### Added

- **Labels on Telegram notices that need you**: 🔐 Needs approval, ❓ Needs answer,
  ❗ Needs you, ⏸ Stalled, ⚠ Stopped, ⛔ All agents limited, above the task's title.

### Changed

- **Only notices that need you make a sound** on the phone: permission prompts, questions
  and every agent being limited. Started, moved, done, stopped and resumed arrive silently,
  as do the bot's confirmations of a button press.

### Fixed

- **Codex 0.160's command approvals** ("Would you like to run the following command?")
  were not recognised as permission prompts: they reached Telegram without Approve / Deny
  and the policy never saw them. They are now, and a command that fits on one line is
  read for the policy; a wrapped one still comes to you.
- **With every agent limited**, the notice named the last agent's reset time even when
  another agent was free sooner and would take the task then. It now names the agent free
  first and whether the task resumes or moves.
- **A Telegram message that is not a reply** reached no agent and got no answer. The bot
  now says so and how to answer.

## [1.0.0] - 2026-10-06

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

[Unreleased]: https://github.com/selorito/baton-herdr/compare/v1.1.0...HEAD
[1.1.0]: https://github.com/selorito/baton-herdr/compare/v1.0.1...v1.1.0
[1.0.1]: https://github.com/selorito/baton-herdr/compare/v1.0.0...v1.0.1
[1.0.0]: https://github.com/selorito/baton-herdr/releases/tag/v1.0.0
