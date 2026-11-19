# 2026-11-16: portability for the Agensi listing

baton is to be sold as a skill on Agensi, installed on buyers' own machines. That made
portability the work of the day.

## Phase 1: the audit

The audit is in `docs/portability-audit.md`. A clean `ubuntu:24.04` showed that the README's
own first command failed:

- uv picked CPython 3.15, which has no `pydantic-core` wheel;
- `zoneinfo` knew no zones without a system database.

## Native Windows, tried on a real machine

The audit first ruled native Windows out. The operator asked for a real try, and kasa
(Windows 11, Turkish locale) gave the answer:

- herdr 0.9.5 serves its API on a named pipe that asyncio reaches;
- panes work, Turkish text included;
- herdr detects agents by process there, which it cannot on WSL1.

baton's suite went from failing on import (`fcntl`) to 1048 passed natively. The changes
are confined to a platform layer (ADR 0025).

## Found on the way

Each of these would have reached a buyer:

- trusted dirs never matched a resolved workdir;
- a BOM in `baton.toml` crashed baton;
- `shlex` ate Windows backslashes;
- glibc-linked release binaries were too new for older distributions (now musl);
- a re-attach loop had written 343 MB of log on kasa.

## Clean-machine tests

`tools/e2e` runs install, then doctor, then one task completed, in four distributions, all
green. The same flow was run on native Windows with install.ps1.

## Open

- The push and release 1.4.0 (CI proof).
- A real agent on native Windows (needs a sign-in).
- A real Mac.
- Phase 3 (troubleshooting).
- Phase 4 (the skill).
