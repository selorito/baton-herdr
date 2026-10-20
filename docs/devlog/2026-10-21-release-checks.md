# 2026-10-21: checks before 1.2.0

The first project with Codex and Claude steps side by side (`p-e7bd73`, four pages, two on
each agent) went through without help: 2 min 11 s from approval to delivery, Codex still
signed in afterwards. One clean run is not proof, so before tagging, three more runs with
faults injected.

## How

A script outside the repository opened its own herdr session (`baton-verify-<pid>`), its
own event log and a policy whose `trusted_dirs` held its directories, and ran
`run_pending` with real agents and a `RecordingNotifier`. The live service, its herdr
session and Codex's real login were not touched. Each task: a file or two in a fresh git
repository.

## Results

| Fault | Expected | Seen |
|---|---|---|
| Codex signed out (`launch_commands`: `env CODEX_HOME=<empty dir> codex`) | sign-in screen recognised, task handed on, notice gives the command | `baton:codex_signed_out` 3 s after launch; `signed_out`, handed to Claude, which finished; the notice said `codex logout && codex login && pkill -f app-server-daemon` |
| Claude killed (SIGKILL) 0.3 s after its process appeared | exit noticed in seconds, task handed on | `crashed` ("exited as it started") 7 s after launch; Codex took it and finished |
| Claude killed mid-turn | crash noticed, session resumed | `baton:agent-process-gone` 0.4 s after the kill; the same Claude session resumed and finished |
| Three Codex sessions at once (`agent_slots = { codex = 3 }`) | launched one after another, none signed out | launches 0.8 s apart, each after the one before showed its screen; update chooser answered in each; all three done in 10–13 s; `codex exec` still answered afterwards |

## Notes

- The first run of the signed-out case stopped on Claude's folder-trust question: the
  task's directory was under `/tmp`, not trusted. Real projects are under `[projects]
  root`, whose trust question baton answers (ADR 0016); with the directory in the policy's
  `trusted_dirs` it was answered too.
- A first kill script matched `pgrep -f` against the command line, and killed something
  other than Claude; it now matches only a process named `claude`.
- Not tested: Codex's refresh token expiring while two sessions run (they share one per
  account). Written in the changelog as a known limitation.
- The hand-off reason says "limited until" for an agent set aside after a sign-in. Left
  for later; the notice's previous line says it is signed out.
