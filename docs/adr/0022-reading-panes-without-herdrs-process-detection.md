---
status: accepted
date: 2026-11-10
---

# Reading panes without herdr's process detection: baton's own reading on WSL1

## Context and Problem Statement

baton learns which agent runs in a pane, and what it is doing, from herdr (ADR 0004):
`pane.get` names the agent, `agent.explain` classifies the screen, `agent.prompt` submits a
prompt, `pane.process_info` lists the foreground processes for crash detection.

herdr names the agent from the terminal's foreground process group: `tpgid` in
`/proc/<pid>/stat`, or in its `child-groups` mode the `/proc/<pid>/task/<tid>/children`
files. WSL1 (a Windows machine without hardware virtualization, which rules out WSL2)
provides neither: measured on Windows 11 build 26200, `tpgid` is always 0 and the
`children` files do not exist. herdr then reports no agent in any pane, in both modes;
`agent.explain` and `agent.prompt` answer `agent_not_found`; `pane.process_info` gives
only `shell_pid`. A `pane.report_agent` from baton is accepted but does not change that.
What still works: panes, screens, keys, the terminal title, and the session id an agent's
integration reports (`agent_session`).

baton cannot run there at all: no attempt ever sees its agent start. How can baton work
where herdr cannot see a pane's processes, without forking herdr (ADR 0003)?

## Decision Drivers

- herdr is not forked or patched (ADR 0003); only its socket API and CLI are used.
- Where herdr can see processes, nothing changes.
- The states baton sees must be the states herdr would report, by the same rules, kept up
  to date as herdr updates them.
- A read must not hang batond: on WSL, reading the cmdline of an exiting process can block
  (herdr src/platform/linux.rs).

## Considered Options

1. Require WSL2 or native Linux; document WSL1 as unsupported.
2. Propose a fallback to herdr upstream (scan `/proc/*/stat` for the shell's descendants)
   and wait for it.
3. Read the panes the way herdr does, from baton, where herdr cannot: the agent from the
   process table, the state from herdr's own rules applied to the screen.

## Decision Outcome

Chosen option: **3**, behind `[herdr] process_detection`: `"herdr"`, `"baton"`, or
`"auto"` (the default), which is `"baton"` on WSL1 (a kernel release ending in
`-Microsoft`) and `"herdr"` elsewhere. Option 2 remains worth proposing; if herdr gains
it, `"auto"` can go back to herdr.

Everything is in `baton_herdr.herdr`; `HerdrPaneHost` keeps its `PaneHost` contract.

- **The agent.** The shell pid from `pane.process_info`; its descendants from the parent
  pid in `/proc/<pid>/stat`, nearest first. The first one that is an agent is the pane's
  agent: by its name (`claude`, `codex`, `opencode`), else by `argv[0]`, else by the script
  a runtime (`node`, `bun`, `python`…) runs. The name is tried first so that cmdline is
  read only when needed, and never for a process that is exiting (`Z`, `X`). The whole
  probe runs off the event loop and gives up after two seconds.
- **The state.** `herdr agent explain --file <screen> --agent <agent> --json` classifies
  the screen (the last 200 rows, as the fixtures were captured) with herdr's own manifests,
  including the ones herdr fetched; it needs no process. A file cannot carry the terminal
  title, so herdr's title rules (region `osc_title`) are applied by baton and ranked
  against the screen's rule by priority, as herdr ranks them. They are read from the
  manifests herdr keeps in `<state_dir>/agent-detection/remote/`; a built-in copy
  (manifests 2026.10.11.1) stands in until herdr has fetched one. The result goes through
  `derive_state` like herdr's own answer, so evidence reads `herdr:rule:<id>`.
- **Prompts.** What `agent.prompt` does: refuse when no agent runs or it is blocked; paste
  the text with `pane.send_input` (bracketed when the terminal asked for it), wait 300 ms,
  press Enter. On WSL1 a write of more than 4 KiB to the pty of an agent in raw mode
  hangs the terminal up and ends the agent with its shell (Claude Code: 4000 bytes pass,
  4500 do not; project steps run about 5 KB). The text is pasted in pieces of at most
  2 KiB; the agent joins consecutive pastes into one prompt.
- **Processes.** The shell's descendants with their command lines, or the shell alone.
- **The log's journal.** WSL1's shared memory does not hold SQLite's WAL together across
  processes: with batond holding the database, every other process opening it (the CLI,
  `baton doctor`, `sqlite3`) failed with `locking protocol` (SQLITE_PROTOCOL). On WSL1 the
  ledger uses the rollback journal (`journal_mode=DELETE`), which locks with `fcntl`;
  elsewhere it stays WAL. A WAL file opened on WSL1 is switched on its next upgrade.
- **Session ids** still come from herdr (`agent_session`). Seen through the process table,
  an agent can appear a moment before its session report: targeting already treats that
  as `UNCONFIRMED` until the id arrives (ADR 0006).

### Consequences

- Good: baton runs on WSL1. On native Linux, the live test
  `test_process_fallback_live.py` reads the same pane both ways in every fake-agent
  scenario; the two agree at every moment herdr's reading is stable. On WSL1 the whole
  live suite passes with the fallback.
- Good: herdr's rule updates reach the fallback without a baton release, the title rules
  included.
- Bad: one `herdr` process per observation of a pane with an agent, about 10 ms. Panes are
  observed a few times per second at most.
- Bad: lifecycle states an integration reports through `pane.report_agent` are not seen,
  because herdr attaches them only to an agent it detects. OpenCode's idle state comes
  that way; on WSL1 an idle OpenCode reads `unknown`, and its turns end by the stall
  timeout. Claude Code and Codex are classified from their screens and titles.
- Bad: herdr's title rules are interpreted by baton, so a new kind of matcher herdr adds
  would be ignored until baton learns it. A rule baton cannot compile is left out, as
  herdr leaves out a manifest it cannot compile.
- Neutral: WSL1 itself kills a distribution's processes when the last `wsl.exe` attached
  to it exits. batond and herdr must run under a `wsl.exe` that stays (a Windows task at
  logon or boot), not detached from an SSH session. WSL1 has no systemd, so
  `baton service install` does something else there; see the amendment below.

## Amendment (2026-11-10): the service on WSL1

On WSL1 `baton service install` (and the service step of `baton setup`) writes one start
script, `~/.local/bin/baton-wsl1-start`, in place of the two systemd units, and prints the
PowerShell command that registers a Windows scheduled task to run it:
`wsl.exe -d <distro> -- <script>`, the distribution from `WSL_DISTRO_NAME`.

- **The script** does what both units do, with the same values `plan_units` computes for
  them: a herdr server in the configured session with `HERDR_CONFIG_PATH`, then, after
  5 s, `baton daemon` with `BATON_HERDR__SESSION` (and `BATON_CONFIG` when pinned); `PATH`
  and `LANG` as in the units. Each runs in a loop that restarts it after it exits, 5 s and
  10 s later, as `RestartSec=` does. The script stays in the foreground, which keeps the
  task's `wsl.exe`, and so the distribution's processes, alive. A second copy started
  while one runs (the task fires at boot and at logon) finds this install's herdr server
  or batond with `pgrep` on their full paths and exits with success, so the task does not
  retry it. Logs go to `~/.local/state/baton/`. The file is replaced by a rename, never
  rewritten in place: bash reads a running script as it goes.
- **The task**: at boot and at logon, principal S4U (runs whether or not the user is
  signed in, without storing a password), no execution time limit, restarted every minute
  after a failure, a new instance ignored while one runs. This is the task that ran baton
  on WSL1 by hand in the run recorded in the devlog of 2026-11-10.
- **baton never runs a Windows command.** Registering a task at boot needs an elevated
  PowerShell, and the user is the one who should grant that; baton prints the command.
  `baton service uninstall` removes the script and prints the commands that stop and
  unregister the task; `baton service status` reports the script and whether the
  processes run, read from `/proc`, and how to ask Windows about the task.
- **`baton doctor`** shows a `wsl1` line (baton reads pane processes, the database uses the
  rollback journal, the task runs the service), warns when `process_detection = "herdr"`
  is forced there, and on an unreachable herdr server recommends the scheduled task, not
  systemd.

Considered and not chosen: having baton call `powershell.exe` or `schtasks.exe` through
WSL interop. It needs elevation baton cannot ask for, interop may be disabled, and a tool
that changes the Windows side unasked is harder to trust than one printed command.
