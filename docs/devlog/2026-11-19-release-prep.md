# 2026-11-19: release 1.4.0 prepared (phases 3 and 4)

## Release binaries

The release matrix already held the five targets: two Linux musl, two macOS and one
Windows MSVC (`36b78c2`, `7a672d5`). Nothing was rewritten. None of them has run on
GitHub yet, because the branch was never pushed. A local container build of
`x86_64-unknown-linux-musl` gives a static-pie binary that runs. macOS and MSVC can only
be built on their own runners. `docs/INSTALL.md` now lists each asset, where it is
built and what has actually been tried. Every system needs `baton-detect`, so no target
could be dropped.

## Documents

- `docs/TROUBLESHOOTING.md`: symptom, cause and fix, per system. It is built from the
  branch's real fixes and `doctor`'s messages. Writing it turned up two wrong instructions,
  both corrected:
  - "run `baton service install` to start the service": on systemd it only writes the
    units, and `baton setup` starts them;
  - a SOCKS proxy claim that no test covers.
- The support matrices (INSTALL, the skill, the listing) now call a system supported only
  when it ran on a real machine:
  - supported: Ubuntu and WSL1;
  - beta: Debian, Fedora and Arch (containers), native Windows (stand-in agent), and
    WSL2, macOS and ARM64 (never run).

## Skill

`skills/baton/SKILL.md`, packed by `tools/skill/pack.sh` with a copy of the troubleshooting
guide, so that the agent needs no download to read it. The script:

- refuses a version that does not match;
- refuses anything that looks like a token;
- gives the same bytes on every run.

The release job attaches `baton-skill-<version>.zip`.

## Open

- Push, CI on every system, a release rehearsal, the merge, and the tag v1.4.0. All of
  these wait for the owner's go-ahead.
- A real Mac, ARM Linux, WSL2, and a real agent on native Windows.
