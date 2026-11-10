# 2026-11-10: `baton service install` on WSL1 (ADR 0022, amended)

The WSL1 machine of [2026-11-10-wsl1.md](2026-11-10-wsl1.md) ran baton from a script
written by hand, `~/.local/bin/baton-wsl1-start`, started by a Windows scheduled task
`baton`. This session turned both into product.

## What changed

- On WSL1, `baton service install` and the service step of `baton setup` write the start
  script from the same plan the systemd units are rendered from, and print the PowerShell
  command that registers the task. The task's settings are those of the hand-made task
  (read with `schtasks.exe /query /tn baton /xml`): boot and logon triggers, S4U, no time
  limit, 9999 restarts a minute apart, `IgnoreNew`. baton runs no Windows command.
- `baton service status` and `baton service uninstall` are new, for systemd and WSL1.
  Status finds the service's processes in `/proc` by their arguments.
- `baton doctor` adds a `wsl1` line and recommends the task on WSL1.

## Differences from the hand-written script

- herdr and baton are called by their full paths, and `PATH` comes from the binaries
  found at install time, as in the units.
- The "already running" check uses `pgrep` on this install's full paths, regex-escaped,
  so another herdr session or a second checkout does not stop the start.
- The file is replaced by a rename. Rewriting it in place while the task's bash runs it
  would change what that bash reads next.

## Tests

- `tests/integration/test_wsl1_start_script.py` runs the generated script with a fake
  herdr and a fake baton: herdr starts with `HERDR_CONFIG_PATH`, batond with
  `BATON_HERDR__SESSION`, and a second copy exits 0 and leaves them running.
- A first version of that test let the fakes `exec sleep`, so their cmdline became
  `sleep 60` and the second copy did not find them. A real herdr and batond keep their
  arguments; the fakes now do too.
- `just check-py` on WSL1: everything passes except the two tests of
  `test_classify_golden.py`, which fail the same way on `feat/herdr-wsl1` without this
  change: the installed `~/.local/bin/baton-detect` answers `unknown time zone
  "US/Eastern"` (Ubuntu 24.04 ships the legacy `US/` zones only in `tzdata-legacy`). No
  Rust toolchain here to look further. The installed baton, `~/.config/baton`, the hand-made
  script and the running task were not touched; `baton service install` was run only
  against temporary homes in the tests.
