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

### Changed

- **License: Business Source License 1.1 from 1.5.0**, in place of Apache-2.0. The code
  stays readable, and may be copied, modified and used outside production. Production use
  needs a license bought on [Agensi](https://www.agensi.io/r/SELMANZDEMIR). Each version
  becomes Apache-2.0 four years after it is published. 1.4.0 and earlier stay Apache-2.0.
  The package metadata (`pyproject.toml`, `Cargo.toml`) says `BUSL-1.1`. Contributions
  need their author's agreement to this (`CONTRIBUTING.md`).
- `CONTRIBUTORS.md` lists who contributed. It restores Berra's (berrayckts) credit for
  the test infrastructure, which a rebase had dropped.

- **The install scripts check baton's wheel** against its SHA-256 (known for 1.4.0, else
  the release's `SHA256SUMS`), as they already did herdr's.
- **Run without a terminal** (by a coding agent, by CI), the install scripts answer every
  question "no" and say so, instead of failing on a missing terminal (`install.sh`) or
  waiting on a question forever (`install.ps1` on an open pipe). `--yes` / `-Yes` accept
  every step. `baton setup`, which asks for the bot token, is then left to the user.
- **`baton setup` refuses without a terminal**, in one line, instead of waiting on its
  first question forever when its input is a pipe that stays open.

- The agent skill is no longer part of this repository or its releases. Its zip was
  attached to the v1.4.0 release for its first hours, and has been removed from it, with
  `SHA256SUMS` regenerated without it; the other files' checksums are unchanged. The
  skill is sold separately. `docs/TROUBLESHOOTING.md`, which it bundles, stays here.

## [1.4.0] - 2026-11-19

Install with one command on Linux, WSL, macOS and Windows; cancel a task, change its tier
or start work in an existing directory by writing to the bot; stop everything with one
word. macOS and native Windows are beta.

### Added

- **`docs/INSTALL.md`** (per system, with the support matrix and the release binaries) and
  **`docs/TROUBLESHOOTING.md`** (symptom, cause and fix per system, and `doctor`'s
  failures).

- **One-command install**: `install.sh` (Linux, WSL, macOS) and `install.ps1` (Windows).
  - They install uv, herdr 0.9.5 (SHA-256 checked) and baton, then run `baton setup`.
  - Each step is shown and asked first; they use no sudo or administrator rights, and
    change nothing outside the user's home.
- `tools/e2e`: a clean machine end to end in a container (Ubuntu 24.04, Debian 12,
  Fedora 42, Arch). The run is install, then `baton doctor`, then a task queued and
  completed in herdr. CI runs it.
- CI runs the Python suite on Linux x86_64 and aarch64, macOS (Apple Silicon and Intel)
  and Windows, and the Rust checks on Linux, macOS and Windows.

- **Native Windows and macOS (ADR 0025).**
  - baton runs on Windows without WSL: herdr's named pipe, a scheduled-task start script,
    and UTF-8 output on every code page.
  - On macOS it runs as two launchd agents.
  - `baton setup` downloads the `baton-detect` of the installed version for this system,
    checked against the release's SHA256SUMS.
  - Releases build Linux, macOS and Windows binaries.

- **Stop words** (ADR 0024). "dur", "iptal", "stop" (alone, or with "hemen", "hepsini",
  an id) cancel the work under way at once, as `/cancel` does, read by baton before the
  assistant and without a button. Several pieces of work running: a button for each, and
  one for all.
- **Delivered pages on Vercel** (ADR 0023). With `[projects] publish = "vercel"` and a
  token in `.env` (`BATON_PROJECTS__VERCEL_TOKEN`), a finished project's page is deployed
  to Vercel as a project of its own, and the delivery notice leads with its public link,
  which opens from any phone. Off by default. `baton project publish p-…` publishes one
  project at any time.
- **WSL1** (ADR 0022). herdr cannot see a pane's processes on WSL1, so it named no agent
  and baton never saw one start. There baton now reads the pane itself: the agent from
  the process table under the pane's shell, the state from herdr's own rules applied to
  the screen (`herdr agent explain --file`) and to the terminal title, prompts pasted the
  way `agent.prompt` pastes them. `[herdr] process_detection` chooses: `"auto"` (the
  default: baton's reading on WSL1 only), `"herdr"` or `"baton"`.
- **`baton service install` on WSL1** (ADR 0022, amended). WSL1 has no systemd, so there
  it writes one start script, `~/.local/bin/baton-wsl1-start`, that runs the herdr server
  and batond and restarts each after it exits, and prints the PowerShell command that
  registers a Windows scheduled task to run it under `wsl.exe`. baton runs no Windows
  command itself. `baton setup` does the same on WSL1.
- **`baton service status` and `baton service uninstall`.** Status says what is installed
  and whether herdr's server and batond run. Uninstall stops, disables and removes the
  systemd units; on WSL1 it removes the start script and prints how to remove the task.
- **`baton doctor` on WSL1** shows a `wsl1` line (pane processes read by baton, the
  rollback journal) and, when the herdr server is unreachable, recommends the scheduled
  task instead of systemd.
- **Cancel a task and change its tier in words** (ADR 0021, amended). "t-1a2b3c4d'yi
  iptal et", "cancel the cafe task", "t-1a2b3c4d ucuz modelde çalışsın": the assistant
  shows a card naming the task and what will happen, and its button does what `/cancel`
  or `/tier` does. baton finds the task from the id or the words; a name that fits no
  open task, or several, gets a question, and the id written in reply answers it.
- **The assistant starts work in an existing directory** (ADR 0021, amended). "~/src/calc
  içinde calc.py'ye bölme ekle", or a project's id, comes as a card with the directory,
  the title and the instructions; Start adds one task there, as `baton task add` does.
  Only a directory inside the policy's `trusted_dirs`, or a project baton made, is
  offered, and only once it exists; anything else is refused with the reason. baton
  creates, moves and deletes no directory for it.

### Fixed

- **baton installs on a fresh machine.** uv picked CPython 3.15, for which `pydantic-core`
  and `aiohttp` have no wheels, and the build from source failed. `requires-python` is
  now `>=3.12,<3.15`, and the install scripts pin 3.12.
- Time zones are known where the system has no tz database (containers, Windows):
  baton depends on `tzdata`.
- The assistant no longer narrows a status answer to an id it invented. Only an id the
  operator wrote counts.
- Linux release binaries are static (musl): one built on a new glibc refused to start on
  older distributions.
- A `baton.toml` saved with a UTF-8 byte order mark (PowerShell 5.1, older Notepad) is
  read. One that is not TOML is named in one line instead of a traceback.
- Agent commands are split by the pane's shell's rules: on Windows a backslash is a path
  separator, so `doctor` found `C:\...\codex.exe` again.

- Telegram works behind a proxy: `HTTPS_PROXY` / `ALL_PROXY` (unless `NO_PROXY` names
  Telegram), or `[telegram] proxy`. aiohttp reads none of them itself.
- `baton doctor` names the system and processor, requires git, holds herdr to the versions
  baton was tested with (0.9.1 to 0.9.5), and points to `baton setup` for baton-detect.
- On WSL1 the start script keeps each log under 20 MB; `daemon.log` had no bound.
- A preview port another program holds moves to the next free one; Chrome is found in its
  macOS and Windows install locations. On Windows the port is bound exclusively: a shared
  bind had let a second batond take the same port, and a held port was not recognised.
- baton-detect reads new usage lines within one poll (2 s) on every system. On macOS and
  Windows the file watcher's events missed appended lines, which then waited for the
  30 s rescan. It now also reads every followed file whose length changed.
- baton-detect knows every zone baton accepts. It falls back to the time zone database
  built into it where the system has none, or lacks old names such as `US/Eastern`
  (Ubuntu moved them to `tzdata-legacy`). Before, it stopped on them while `doctor`
  said the zone was fine.

- A trusted directory written with `~`, through a link, or on Windows without its drive
  now matches the workdir it holds (both sides are resolved).
- The assistant takes a Windows path (`C:\Users\…`) as absolute.

- An attempt waiting for a person no longer loops: a re-attach that changed nothing does
  not wake the daemon, so it is looked at again after the idle time, not ~once a second.

- **WSL1: the CLI could not open the log batond held** (`locking protocol`). SQLite's WAL
  needs shared memory WSL1 does not share across processes; on WSL1 the database now uses
  the rollback journal.
- **WSL1: a prompt over 4 KB ended the agent and its pane.** WSL1 hangs a terminal up on
  a write that large; every project step failed with "the claude pane is gone". Long
  prompts are now pasted in 1 KiB pieces, 0.25 s apart, so that the agent drains its
  terminal while it starts.
- **A subscription that ended mid-turn failed the task's run.** herdr's watch subscribed
  again after `events_lost`, but not after its connection ended: the run stopped with
  `herdr closed the connection` until batond's next cycle re-attached. It now subscribes
  again and reads the pane, as after lost events.

## [1.3.0] - 2026-10-23

Ask baton in your own words, run routine work on a cheaper model, and start tasks side by
side without piling them onto an agent near its limit.

### Added

- **Ask baton in your own words** (ADR 0021). With `[assistant]` set to a model Ollama
  serves on this machine, a plain message in Telegram is read by it: "t-1a2b3c4d neden
  takıldı?", "what is running?". The model only interprets the message; the answer comes
  from baton's log. A "why" is explained by the model from a digest of the log, citing the
  events it relied on (`Sources: #12, a-1a2b3c4d`); a citation the digest does not hold is
  dropped, and the digest never holds instructions, screens, directories or secrets.
  Other questions get the facts in a fixed form. One message at a time, within
  `timeout_seconds`; Telegram shows "typing…" meanwhile. Answers in the language of the
  message (`language`). Without `[assistant]`, plain messages are answered as before;
  `baton doctor` checks Ollama and the model.
- **Start a project in your own words** (ADR 0021). "kafem için menülü bir site yap"
  comes back as a card: the request as `/new` would get it, where the project goes, the
  agent a task would go to now and why, and the tier, with **Start**, **Edit** and
  **Cancel**. Start does exactly what `/new` does, once, however often it is pressed; the
  plan then comes for your approval as before, so nothing is coded unasked. Edit takes
  your next message as the change and replaces the card. A request missing something gets
  a question instead of a card. Cards can be pressed for `card_ttl_minutes` (30) and are
  forgotten on a restart.
- **Economy tier** (ADR 0020). Routine work can run on a cheaper model of the same agent:
  `[tiers] roles = { step = "economy" }` and `[tiers.economy] claude = { model = "haiku" }`
  build a project's pages on Haiku and plan them on the default model. An economy task goes
  first to an agent with an economy profile, and runs premium when none has one, or waits
  (`economy_fallback`). The reason line says which: `… Economy: haiku.` A resumed session
  keeps its tier. `baton task add --tier economy|premium` sets it for one task. Without
  `[tiers]` nothing changes. Economy shares the agent's account and limits; it spends them
  more slowly. Budget reservations (ADR 0019) are learned per tier. New fields:
  `task.created.tier`, `attempt.started.tier` and `.backend`.
- **An economy task that fails moves up** (ADR 0020). When an economy attempt stalls,
  crashes or fails to start (`[tiers.escalation] on`), `after` times, the task goes on in
  premium on the same agent, in a fresh session with the hand-off prompt. A crash is
  resumed in economy first, within `max_failure_resumes`. An economy attempt counts as
  stalled after `economy_stall_minutes` (10). The step is logged as `task.escalated` and
  the notice says `⬆ Moved up to premium on claude.` A task moves up once and does not go
  back down by itself.
- **Tiers from Telegram and the CLI** (ADR 0020). `/tier t-… economy|premium` and
  `baton task tier` set the tier a task runs in from its next attempt (`task.tier_changed`);
  setting economy undoes a move up. `/new … #economy` (or `#premium`) and
  `baton project new --tier` set a whole project's tier: its plan, steps and merges.
  `/status` marks economy tasks `(economy)` and moved-up ones `(⬆premium)`, and
  `baton status` shows economy attempts as `claude/economy:…`; premium lines read as
  before.
- **`baton setup` offers the assistant** when Ollama is installed: it asks for the model
  (`qwen3.5:4b` by default), pulls it if needed, and writes `[assistant]`.
- **Telegram's command menu.** The bot registers its commands, so typing "/" in the chat
  lists them with what each does. `/help` tells about plain messages.

### Changed

- **Tasks started at once no longer pile onto an agent near its limit** (ADR 0019). Every
  running attempt reserves what it is expected to spend still: the median of its agent's
  last finished attempts of the same kind, less what its session has spent so far. The
  reserve is compared with what is left after that, and an agent below it runs one task
  at a time. Before, four tasks assigned in the same second all saw the same "12% left"
  and could all hit the limit. The reason line and `/budget` show it: `62% left, ~41%
  after 2 running`. `task.agent_chosen` budget notes gain `running` and
  `reserved_percent`.

### Known limitations

- The assistant runs on the machine's CPU unless a GPU is there: about 10 s an answer, and
  a model holds 3.8 GB (`qwen3.5:4b`) to 6.7 GB (9B) while loaded
  (`docs/devlog/2026-10-22-assistant-on-ollama.md`). One message is answered at a time.
- Confirmation cards live in memory: a restart of batond forgets them, and a press then
  says the card has expired. Nothing is started from a forgotten card.
- Work in an existing directory cannot be asked for in words yet; `baton task add` does it.

## [1.2.0] - 2026-10-21

Agents work side by side: a queue of tasks, and a project's independent steps, finish about
three times sooner. Checked before release against real Claude Code and Codex sessions in a
throwaway herdr session, with faults injected (`docs/devlog/2026-10-21-release-checks.md`).

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
  cooldown, and the notice says what to run: `codex logout && codex login && pkill -f
  app-server-daemon`. Signing in alone was not enough: Codex's background app server kept
  the revoked token, and new sessions asked again until it was stopped.
- **A plan step a little long no longer loses the whole plan.** The planner is told to keep
  each step under 3,000 characters, and a step is read up to 12,000; a plan whose step
  had 4,001 characters used to be rejected after the planning turn had been spent.

### Added

- **`baton agent ready <agent>`** uses an agent again that baton set aside after a limit
  or a sign-in, once you have fixed it, instead of waiting out the cooldown (ADR 0018).
  Recorded in the log as `agent.released`, with who said so.
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

### Known limitations

- **Codex sessions of one account share one refresh token.** baton starts them one after
  another, which keeps two from refreshing it at the same start (seen live before 1.2).
  Two sessions that already run can still both refresh it when it expires during a long
  turn; Codex then signs out. Not reproduced yet. If it happens, a session that shows the
  sign-in screen is handed on as above, and one that only stops is resumed after
  `stall_minutes`; sign in again with the command in the notice.
- The reason line of a hand-off says "limited until" for an agent set aside after a
  sign-in too; the line above it says why.

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

[Unreleased]: https://github.com/selorito/baton-herdr/compare/v1.4.0...HEAD
[1.4.0]: https://github.com/selorito/baton-herdr/compare/v1.3.0...v1.4.0
[1.3.0]: https://github.com/selorito/baton-herdr/compare/v1.2.0...v1.3.0
[1.2.0]: https://github.com/selorito/baton-herdr/compare/v1.1.0...v1.2.0
[1.1.0]: https://github.com/selorito/baton-herdr/compare/v1.0.1...v1.1.0
[1.0.1]: https://github.com/selorito/baton-herdr/compare/v1.0.0...v1.0.1
[1.0.0]: https://github.com/selorito/baton-herdr/releases/tag/v1.0.0
