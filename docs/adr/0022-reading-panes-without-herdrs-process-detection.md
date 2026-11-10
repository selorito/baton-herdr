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
  press Enter.
- **Processes.** The shell's descendants with their command lines, or the shell alone.
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
  logon or boot), not detached from an SSH session. `baton service install` (systemd) does
  not apply; WSL1 has no systemd.
