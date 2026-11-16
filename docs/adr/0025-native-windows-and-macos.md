---
status: accepted
date: 2026-11-16
---

# Native Windows and macOS, behind one platform layer

## Context and Problem Statement

baton was built and tested on Linux, and runs on WSL1 since ADR 0022. It is to be sold to
people who install it on their own machine, whatever it is. The portability audit
(`docs/portability-audit.md`) found that it could not start on native Windows. `fcntl`,
Unix sockets, loop signal handlers and FIFOs were the blockers. It also found that on
macOS it downloaded a Linux binary and knew no service but systemd.

A first reading concluded that native Windows needed a port too deep to do, because herdr's
API transport there was unknown. The operator asked for it to be tried on a real machine
before being ruled out. On Windows 11 (kasa, herdr 0.9.5, CPython 3.12) these held:

- herdr serves its API on a named pipe, `\\.\pipe\<socket path>`, and asyncio's proactor
  `create_pipe_connection` speaks baton's protocol to it unchanged;
- panes open, read and take input, Turkish text included;
- herdr detects agents by their foreground process, which WSL1 could not do;
- with the facilities above replaced, baton's whole suite passes there: 1048 passed, and
  40 POSIX-only tests skipped.

How should baton support Windows and macOS without spreading `sys.platform` through its
logic, and without changing its architecture?

## Decision Drivers

- No rewrite: the event log, the scheduler and the herdr protocol stay as they are.
- Platform differences live in one place, and every caller asks there.
- Nothing a buyer cannot see: a service is written and shown, Windows commands that need
  an administrator are printed, not run.
- What was not tried on real hardware is not called supported.

## Considered Options

1. Support Windows through WSL only, and refuse natively.
2. Port natively, with the differences confined to a platform layer (`core/platform.py`)
   and to the one module that owns each concern.

## Decision Outcome

Chosen option: **2**.

`core/platform.py` names the system, the processor and the release target, and holds the
pure helpers that differ by system:

- where herdr keeps its sockets (`%APPDATA%\herdr` on Windows);
- the pipe name;
- an exclusive file lock (`msvcrt` or `fcntl`);
- quoting for a pane's shell (POSIX or PowerShell).

Each concern stays with its owner:

- **`herdr/transport.py`:** a named pipe on Windows. It retries while every pipe instance
  is busy.
- **`ledger/migrate.py`:** the platform lock.
- **`daemon.py`:** the CLI wakes batond through the wake file's modification time where
  there are no FIFOs.
- **`cli.py`:**
  - signal handlers fall back to `signal.signal`;
  - streams are UTF-8 on Windows, whose code page (cp1254 in Turkey) has no emoji.
- **Agent launch:** commands typed into a pane are quoted for its shell. Claude's
  `--settings` names a file instead of carrying JSON, which Windows PowerShell 5.1 strips
  of its quotes.
- **`service.py`:**
  - launchd agents on macOS;
  - on Windows, a PowerShell start script run by a scheduled task. Like WSL1's, the
    administrator's command is printed.
- **`setup.py`:**
  - downloads the `baton-detect` of the installed version, for this system's target
    (`.exe` on Windows);
  - checks it against the release's `SHA256SUMS`.
- **Release:** builds Linux (x86_64, aarch64), macOS (arm64, x86_64) and Windows
  (x86_64) binaries, each on its own system, because `rusqlite` compiles SQLite's C.
- **Paths compared by meaning, not spelling:**
  - an absolute path is absolute by the system's rules;
  - trusted directories are resolved like the workdir they are compared with.

### Consequences

- Good:
  - one code base runs on Linux, WSL1/WSL2, macOS and Windows;
  - the differences can be read in one file.
- Good: two fixes found on the way also help Linux.
  - A trusted directory written with `~` or through a link now matches its workdir.
  - The re-attach loop fix (`a022921`) was found during the same audit.
- Bad:
  - native Windows and macOS are not yet run end to end with real agents, so they are
    marked beta until they are;
  - Windows PowerShell's quoting and the agents' Windows builds (Codex runs commands in
    PowerShell, so read-only policy rules do not match) are new surfaces.
- Neutral: tests that exercise a POSIX facility on purpose are marked `posix_only`, and
  what Windows does instead is tested on its own.
