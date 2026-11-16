# Portability audit

Date: 2026-11-14. Scope: everything a buyer needs to install and run baton on their own
machine (Linux distributions, macOS on Intel and Apple Silicon, Windows with WSL, native
Windows), on x86_64 and aarch64. Phase 1 of the Agensi preparation: **analysis only, no code
changed**. Line numbers refer to branch `feat/assistant-stop` at `a022921`.

Method: the repository was read for platform assumptions (paths, OS calls, external tools,
ports, versions), the lock file was checked for native wheels, the published GitHub
release was inspected, and the README quickstart was run as written in a clean
`ubuntu:24.04` container. Nothing here was tested on macOS, native Windows, WSL2 or
aarch64; those cells say so.

Severity: **blocker**: a buyer following the docs cannot get baton running; **major**: it
runs, but a common environment breaks a feature or the buyer hits a confusing failure;
**minor**: an edge case or a rough edge in the docs.

## Summary

| # | Finding | Where it breaks | Severity |
|---|---------|-----------------|----------|
| B1 | The README quickstart fails on a fresh machine (Python 3.15 is picked, `pydantic-core` has no wheel for it) | every OS | blocker |
| B2 | What would be sold is not on GitHub: the assistant, WSL1 support, Vercel publishing and stop words exist only on unpushed local branches | every OS | blocker |
| B3 | `baton setup` downloads a Linux binary on macOS, and no macOS binary is released | macOS | blocker |
| B4 | `baton service install` knows only systemd (and WSL1) | macOS | blocker |
| B5 | baton cannot start on native Windows (`fcntl`, Unix sockets, signals, FIFOs); herdr itself works there, so a port is feasible | native Windows | blocker |
| I1 | `timezone` fails as `unknown zone 'UTC'` where the system has no tz database | containers, minimal installs | major |
| I2 | `baton-detect` is downloaded from `latest`, unverified, with `curl` | every OS | major |
| I3 | The WSL1 start script appends to `daemon.log` forever | WSL1 | major |
| I4 | The Telegram bot ignores `HTTPS_PROXY` | corporate networks | major |
| I5 | herdr's version is shown, never checked; only 0.9.1 was tested, 0.9.5 is current | every OS | major |
| I6 | CI runs on Ubuntu only | macOS, Windows | major |
| I7 | Installing herdr is left to the buyer, with no command | every OS | major |
| I8 | Gemini CLI is not supported (ADR 0008) but appears in the product pitch | listing | major |
| I9 | Two users on one machine collide on the preview port and on one bot token | shared machines | major |
| M1 | Screenshots miss Chrome on macOS | macOS | minor |
| M2 | `baton-detect`'s default directories need `HOME` | native Windows | minor |
| M3 | macOS limits Unix socket paths to 104 bytes | macOS, long home paths | minor |
| M4 | Release binaries are glibc-only | Alpine (musl) | minor |
| M5 | `doctor`'s hints assume a source checkout and Rust | every OS | minor |
| M6 | The service PATH omits Homebrew's `/opt/homebrew/bin` | macOS | minor |
| M7 | The README names Linux and WSL only; the operator's guide is in Turkish only | docs | minor |
| M8 | No stated hardware minimums (the local assistant model needs ~3.5 GB) | low-RAM machines | minor |

## Blockers

### B1. The quickstart install fails on a fresh machine

- **What.** `requires-python = ">=3.12"` has no upper bound, so `uv tool install
  git+https://github.com/selorito/baton-herdr` downloads the newest CPython, 3.15.2. A
  `git+` install resolves fresh and ignores `uv.lock`. `pydantic-core` 2.46.5 (like
  `aiohttp` 3.14.3) has wheels for CPython 3.12–3.14 only. uv therefore builds it from
  source, which needs Rust, and the build fails.
- **Where.** `pyproject.toml:8`; README "Quickstart" (`README.md:52-54`).
- **Breaks.** Any machine without a system Python 3.12–3.14 that uv would prefer. Verified:
  this was the first command a buyer runs, in a clean `ubuntu:24.04`, and it failed:
  ```
  Downloading cpython-3.15.2-linux-x86_64-gnu
  error: Failed to build `pydantic-core==2.46.5`
    cause: Call to `maturin.build_wheel` failed (exit status: 1)
           Python reports SOABI: cpython-315-x86_64-linux-gnu
  ```
  `uv tool install --python 3.12 …` in the same container installed `baton 1.3.0`.
- **Fix.** Bound the range to what the lock has wheels for (`>=3.12,<3.15`), pin the
  interpreter in the documented command (`--python 3.12`), and raise the bound when the
  wheels exist.

### B2. The product is not what GitHub serves

- **What.** `main` was last pushed on 2026-10-23 (v1.3.0). Everything since lives on local
  branches `feat/herdr-wsl1` and `feat/assistant-stop`:
  - the Telegram assistant (ADR 0021);
  - WSL1 support (ADR 0022);
  - Vercel publishing (ADR 0023);
  - stop words (ADR 0024);
  - the re-attach loop fix.
  A buyer who installs from GitHub or the latest release gets none of these.
- **Where.** The GitHub repository and its releases (`v1.3.0`).
- **Breaks.** Everywhere: the listing would describe features the buyer cannot install.
- **Fix.** Merge to `main` through CI and release 1.4.0 before listing. The skill should pin
  that version.

### B3. macOS gets a Linux `baton-detect`

- **What.** `baton setup` builds the download target as `f"{arch}-unknown-linux-gnu"`
  whatever the OS is. The release workflow publishes only `x86_64-unknown-linux-gnu` and
  `aarch64-unknown-linux-gnu`.
- **Where.** `src/baton_herdr/cli.py:617-623`, `.github/workflows/release.yml` (matrix).
- **Breaks.** macOS, Intel and Apple Silicon. The downloaded file cannot execute
  ("exec format error" / "bad CPU type"), so `doctor` fails `baton-detect` and `detector`.
  Without the detector, baton cannot recognise limits or prompt kinds.
- **Fix.**
  - Choose the target from `platform.system()` and `platform.machine()` in one place (the
    platform layer, `core/platform.py`).
  - Build `aarch64-apple-darwin` and `x86_64-apple-darwin` on native macOS runners.
    `rusqlite` is `bundled` and compiles SQLite's C, so cross-compiling from Linux needs a
    C cross-toolchain; native runners avoid that.
  - Fail with a clear message on targets with no binary.

### B4. No service on macOS

- **What.** `baton service install` writes systemd user units and runs `systemctl` and
  `loginctl`. The only alternative is the WSL1 start script.
- **Where.** `src/baton_herdr/cli.py:648-677`, `src/baton_herdr/service.py` (units),
  `src/baton_herdr/doctor.py:309-321` (hint).
- **Breaks.** macOS. `systemctl` is not found, so `baton setup`'s last step fails and baton
  does not survive a reboot or a logout.
- **Fix.** A launchd LaunchAgent (`~/Library/LaunchAgents/…plist`, `KeepAlive`, logs under
  `~/Library/Logs/baton/`), written next to the systemd and WSL1 cases in `service.py` and
  chosen by the platform layer. Its plan includes the herdr server, like the systemd units.

### B5. Native Windows: a traceback instead of a message

- **What.** baton depends on POSIX facilities in several places:
  - `import fcntl` at module level (the migration lock);
  - `asyncio.open_unix_connection` (herdr's socket);
  - `loop.add_signal_handler`, which raises `NotImplementedError` on Windows;
  - `os.mkfifo` (the CLI's wake-up FIFO);
  - `/proc` (WSL1 process table).

  `baton daemon` cannot start, and the failure is a Python traceback.
- **Where.** `src/baton_herdr/ledger/migrate.py:5,35`, `src/baton_herdr/herdr/transport.py:89`,
  `src/baton_herdr/cli.py:527`, `src/baton_herdr/daemon.py:371`.
- **Breaks.** Native Windows (PowerShell, cmd).
- **Re-examined on 2026-11-16, on a real Windows 11 machine (kasa, build 26200,
  Turkish locale).** herdr 0.9.5 for Windows was used (checksum verified), with CPython
  3.12.16 from uv. Everything ran in `C:\baton-wintest`, beside the WSL service, which
  was not touched.
  - **herdr's API works from Python.** On Windows it is a named pipe, not a Unix socket.
    `herdr.sock` is a marker file, and the pipe is `\\.\pipe\<the socket path>`
    (herdr `src/ipc.rs`, `interprocess`'s `GenericNamespaced`). The asyncio call
    `loop.create_pipe_connection` (Proactor) spoke baton's newline-delimited JSON to it:
    `workspace.list` answered.
  - **herdr's config directory is `%APPDATA%\herdr`**, not `~/.config/herdr`. The
    session socket was `C:\Users\<user>\AppData\Roaming\herdr\sessions\<name>\herdr.sock`.
  - **Panes work.** `workspace.create`, `pane.get`, `pane.send_text`, `pane.send_keys`,
    `pane.read` and `pane.close` all worked. Turkish text round-tripped (`ğüşİ`). The
    pane's shell is Windows PowerShell 5.1.
  - **Agent detection works, unlike WSL1.** A process named `claude.exe` in the pane was
    reported as `agent: claude, status: idle`. `pane.process_info` gave its pid, argv and
    cwd, and `agent.explain` evaluated herdr's rules.
  - **baton's suite, run on native Windows.**
    - It does not start: `import fcntl` fails.
    - With stand-in `fcntl`, `termios` and `tty` modules (sandbox only) and
      `PYTHONUTF8=1`, the result was **803 passed, 56 failed, 18 errors, 163 skipped**.
      The skips are mostly tests that need `baton-detect`, not built for Windows there.
    - Without UTF-8 mode a test file failed to decode under the Turkish code page
      (cp1254).
  - **The failures group into a few causes:**
    - the herdr fakes in the tests use `asyncio.start_unix_server` (17);
    - `git` was not installed (`git init`, WinError 2);
    - the wake FIFO (`os.mkfifo`, `os.O_NONBLOCK`);
    - POSIX file modes (`0o600` for `.env`, `0o755` for the binary);
    - paths written or parsed with `/`;
    - the systemd units.
- **Further risks found by reading.**
  - Agents are started by typing a command into the pane, quoted for a POSIX shell
    (`shlex.quote`). In PowerShell 5.1 single quotes nest differently, and embedded double
    quotes are stripped from native arguments. Claude's `--settings '<json>'` would arrive
    broken, so passing the settings as a file avoids it
    (`src/baton_herdr/adapters/claude.py:71`, `src/baton_herdr/scheduler/runner.py:534`).
  - The screen rules and fixtures were all captured on Linux terminals, and ConPTY may
    render differently.
  - Codex runs its commands in PowerShell on Windows, so the policy's patterns (`ls`,
    `cat`) do not match and everything asks. That is safe, but it is a nuisance.
- **Fix (revised).** A native Windows port is feasible without changing the architecture.
  The changes are confined to:
  - the transport (a named pipe);
  - the herdr path resolver (`%APPDATA%`);
  - the migration lock (`msvcrt` or a lock file);
  - signal handling;
  - the wake channel;
  - the service (a Windows scheduled task);
  - permissions (an ACL instead of chmod);
  - the agents' launch quoting.

  Until it is done, the CLI refuses on `win32` with a pointer to WSL. Before it is called
  supported, it must be tried end to end with a real Claude Code on Windows, which needs
  Git for Windows and a one-time sign-in by the user.

## Major

### I1. No tz database: `unknown zone 'UTC'`

- **What.** `zoneinfo` reads the system tz database. Minimal images (Docker's `ubuntu`,
  `debian:slim`) have none, and baton does not depend on the `tzdata` package. The first
  `doctor` in the clean container failed `timezone` on the default zone: `FAIL timezone
  unknown zone 'UTC'`.
  - Scheduling uses the zone to read agents' local-time reset messages ("resets 3:45pm").
    A wrong zone delays or advances every resume after a limit.
- **Where.** `src/baton_herdr/doctor.py:614-620`, `pyproject.toml` (dependencies).
- **Breaks.** Containers and minimal server installs. Native Windows would hit it too.
- **Fix.** Depend on `tzdata`, which is pure Python, used only when the system has none.

### I2. `baton-detect` is fetched from `latest`, unverified, with `curl`

- **What.**
  - `baton setup` downloads `…/releases/latest/download/baton-detect-<target>`. When the
    installed baton is not the latest release, the binary's version differs, and `doctor`
    then fails it for not matching.
  - The release publishes `SHA256SUMS`, but setup does not check it.
  - setup runs the `curl` program, which the clean Ubuntu image did not have.
- **Where.** `src/baton_herdr/setup.py:31-32` (URL), `src/baton_herdr/setup.py:222` (`curl`),
  `src/baton_herdr/doctor.py:426-435` (version match).
- **Breaks.** Every OS: a version mismatch after any release, or a missing `curl`. An
  unverified download is also exactly what a security review flags.
- **Fix.** Download `releases/download/v<own version>/…`, check it against that release's
  `SHA256SUMS`, and use `urllib` instead of `curl`. Print the URL before downloading, as
  now.

### I3. WSL1 logs grow without bound

- **What.** The WSL1 start script appends batond's and herdr's output to files that
  nothing ever rotates. systemd's journal rotates, but this path has no journal. On kasa
  `daemon.log` reached 343 MB in four days; most of it came from the re-attach loop fixed
  in `a022921`, but the growth has no bound either way.
- **Where.** `src/baton_herdr/service.py:295,304`.
- **Breaks.** WSL1. A launchd agent (B4) would need the same care.
- **Fix.** In the start script, move a log aside at start when it exceeds a size (for
  example 20 MB, keeping one old copy). No new dependency is needed.

### I4. Telegram ignores proxies

- **What.** Every `aiogram.Bot` is created without a session. aiohttp does not read
  `HTTPS_PROXY` unless asked, so behind a proxy the bot never connects. Vercel publishing
  and the setup's Telegram calls use `urllib`, which does honour the proxy variables, so
  the failure is partial and confusing.
- **Where.** `src/baton_herdr/telegram/notifier.py:165`, `src/baton_herdr/doctor.py:79`,
  `src/baton_herdr/daemon.py` (bot start).
- **Breaks.** Corporate and university networks. These are exactly where a demo at a
  company happens.
- **Fix.** Add a `[telegram] proxy` setting, defaulting to `HTTPS_PROXY`, passed as
  `AiohttpSession(proxy=…)` in one helper used by all three places. Have `doctor` name the
  proxy it used.

### I5. herdr's version is not checked

- **What.** `doctor` prints `herdr --version` but compares it with nothing. The research
  and every capture used herdr 0.9.1 (protocol 22); the current herdr release is 0.9.5,
  never tried with baton.
- **Where.** `src/baton_herdr/doctor.py:288-299`, `docs/research/agents.md:21`.
- **Breaks.** A buyer installs today's herdr. If its protocol changed, baton fails in ways
  `doctor` does not explain.
- **Fix.**
  - Fail below 0.9.1. Warn above the newest version tested, with the tested range written
    in one constant.
  - Run `just smoke` against 0.9.5 and record the result.

### I6. CI covers Ubuntu only

- **What.** `ci.yml` runs the Python and Rust checks on `ubuntu-latest`. The release
  builds Linux binaries only. The Python suite has never run on macOS.
- **Where.** `.github/workflows/ci.yml`, `.github/workflows/release.yml`.
- **Fix.** A matrix as follows:
  - Python and Rust on `ubuntu-latest`, `ubuntu-24.04-arm`, `macos-latest` (arm64) and
    `macos-13` (x86_64);
  - Rust plus a test that the CLI refuses cleanly on `windows-latest`;
  - release binaries for the four Unix targets.

### I7. herdr has to be installed by hand

- **What.** The README requires herdr ≥ 0.9.1 and links its repository, but gives no
  command. `baton setup` checks only for its own binary. herdr ships per-platform binaries
  (`herdr-linux-x86_64`, `-linux-aarch64`, `-macos-x86_64`, `-macos-aarch64`).
- **Where.** `README.md:47-50,464-470`, `src/baton_herdr/setup.py:190-206`.
- **Fix.**
  - Give exact per-OS commands in `docs/INSTALL.md`: the asset name, `~/.local/bin`, and a
    checksum check.
  - Let the install script offer the same download, shown and confirmed, never silent.

### I8. Gemini CLI in the pitch, not in baton

- **What.** The product description lists Gemini CLI. baton v1 supports Claude Code, Codex
  CLI and OpenCode; Gemini is "after v1" (ADR 0008). The agent list is a closed
  `Literal`.
- **Where.** `src/baton_herdr/core/config.py:180`, `README.md:416`.
- **Fix.** Leave Gemini out of the listing and the skill until an adapter exists.

### I9. Two users on one machine

- **What.**
  - The preview server binds a fixed port, 8765, so the second user's preview cannot
    start; `doctor` reports it, but the cause is not named.
  - Telegram allows one `getUpdates` poller per bot token, so two daemons with one token
    take each other's updates.
  - Everything else is per user under `~` (XDG paths, herdr's socket, the database).
- **Where.** `src/baton_herdr/core/config.py:301`.
- **Fix.**
  - Document one bot per user.
  - Have the preview fall back to a free port when 8765 is taken, and print the link.

## Minor

- **M1. Chrome on macOS.** Screenshots look for `chromium` and `google-chrome` on `PATH` and
  for Playwright's `chrome-linux`. Both miss `/Applications/Google Chrome.app`, so macOS
  deliveries come without a picture. Fix: add the app bundle paths.
  `src/baton_herdr/projects/deliver.py:69-73`.
- **M2. `HOME` in `baton-detect`.** The default Claude, Codex and OpenCode directories come
  from `HOME`, which native Windows does not set. This is irrelevant under WSL and
  harmless while native Windows is unsupported (B5).
  `crates/baton-detect/src/usage/collect.rs:44-54`.
- **M3. Socket path length.** macOS caps `sun_path` at 104 bytes (Linux: 108).
  `~/.config/herdr/sessions/<name>/herdr.sock` fits unless the home directory or
  `XDG_CONFIG_HOME` is long. Fix: have `doctor` warn above 100 bytes.
  `src/baton_herdr/core/config.py:146-153`.
- **M4. musl.** Release binaries link glibc, so `baton-detect` does not run on Alpine. Fix:
  document Alpine as unsupported, or add `*-unknown-linux-musl` targets.
- **M5. Hints for developers.** `doctor`'s fixes for `baton-detect` say `cargo install
  --path crates/baton-detect --locked (from the baton-herdr checkout)`. A buyer has neither
  a checkout nor Rust. Fix: point to `baton setup` or the release download first.
  `src/baton_herdr/doctor.py:435,595,602`.
- **M6. Homebrew's PATH.** The service PATH falls back to `/usr/local/bin:/usr/bin:/bin`.
  On Apple Silicon, Homebrew installs to `/opt/homebrew/bin`. This matters once B4 adds
  launchd. `src/baton_herdr/service.py:64`.
- **M7. Docs.** The README states Linux, WSL2 and WSL1, and says nothing about macOS.
  `docs/kurulum.md`, the step-by-step guide, is Turkish only.
- **M8. Hardware.** Nothing states minimums. The optional assistant's default model
  (`qwen3.5:4b`) needs about 3.5 GB of RAM or VRAM; on kasa (GTX 1050 Ti, Vulkan) it
  answers at about 13 tokens/s. Each agent CLI holds a few hundred MB. Fix: state them, and
  say that the assistant is optional.

## Checked and found portable

- **No user-specific paths in `src/`.** Configuration, data and state follow
  `XDG_CONFIG_HOME`, `XDG_DATA_HOME` and `XDG_STATE_HOME`, with the `~/.config`,
  `~/.local/share` and `~/.local/state` defaults. herdr's socket lookup mirrors herdr's
  own. The only absolute paths are the systemd fallback PATH (M6) and Linux browser
  locations (M1).
- **Python's native dependencies have wheels for every target.** `pydantic-core`,
  `aiohttp`, `greenlet`, `pyyaml` and `markupsafe` have wheels for Linux x86_64 and
  aarch64 (glibc and musl), macOS arm64 and x86_64, and Windows, for CPython 3.12–3.14.
  Outside that range, see B1.
- **The Rust code is OS-neutral.** There is no `cfg(target_os)`, no `unsafe`, and no
  `/proc`. `notify` picks inotify or FSEvents itself. The only C is `rusqlite`'s bundled
  SQLite, which needs a C compiler for each target.
- **Locale and Turkish text.** The service sets `LANG` (falling back to `C.UTF-8`). Files
  are read and written as UTF-8. Turkish casing for stop words is handled explicitly.
- **WSL1 specifics are isolated.** They sit behind `core/platform.running_on_wsl1()`. Its
  callers choose a behaviour: the process table, the journal mode, the service. That is
  the platform layer the other fixes should extend.
- **`baton doctor` exists and covers a lot.** It already checks the config, database,
  herdr and its server, the agents on `PATH`, `baton-detect` and its version, Claude's
  limits, the policy, projects, the preview, the time zone, Telegram and the assistant.
  What it lacks is listed above: the OS and architecture, herdr's version range, proxies,
  and git.

## Support matrix

Today, before any fix. "Tested" means run end to end by us.

| OS | x86_64 | aarch64 |
|----|--------|---------|
| Ubuntu 24.04 | **supported**: development machine, and the install probe with `--python 3.12` | not tested (a binary is released, never run) |
| Debian, Fedora, Arch | not tested (expected to behave like Ubuntu once B1 and I1 are fixed) | not tested |
| Alpine (musl) | not supported (M4) | not supported |
| macOS | not supported (B3, B4) | not supported (B3, B4) |
| Windows + WSL1 | **supported**: kasa, unattended since 2026-11-10, reboots included | not tested |
| Windows + WSL2 | partial: documented, not tested by us | not tested |
| Windows, native | not supported today (B5); herdr, its pipe API and agent detection verified on kasa, 803/877 tests pass with stand-ins | not tested (herdr ships arm64 ConPTY) |

## Needs testing by hand

These cannot be proven from here, and have to be tried on real hardware:

1. macOS on Apple Silicon and on Intel:
   - herdr, then baton, then `baton doctor`, then a task to completion;
   - the launchd agent surviving a logout and a reboot;
   - Gatekeeper's quarantine on a downloaded `baton-detect` ("cannot be opened because the
     developer cannot be verified": `xattr -d com.apple.quarantine`, or a signed binary).
2. Windows 11 with WSL2:
   - the whole flow, and systemd inside WSL2 (`/etc/wsl.conf`, `systemd=true`).
3. Linux on aarch64 (a Raspberry Pi 5 or an ARM VM), end to end.
4. A network behind an HTTP proxy, with the Telegram bot.
5. herdr 0.9.5 with `just smoke`.

## Proposed phase 2, in order

1. B2: merge and release 1.4.0, so that everything after it ships.
2. B1, I1: bound the Python version, depend on `tzdata`.
3. B5: refuse on native Windows with a clear message.
4. The platform layer: one function naming the OS, architecture and release target, used
   by setup, service and doctor. Then B3 and the release matrix, and B4 (launchd).
5. I2, I5, I4, I3: a verified, versioned download; herdr's version range; the Telegram
   proxy; a log size cap.
6. `doctor` additions: OS and architecture, herdr range, proxy, git.
7. An install script, `docs/INSTALL.md`, `docs/TROUBLESHOOTING.md`, the CI matrix, and
   clean-container runs on Ubuntu, Debian, Fedora and Arch.
