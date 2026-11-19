# Installing baton

baton runs on your own computer. It starts coding agents (Claude Code, Codex CLI,
OpenCode) inside [herdr](https://github.com/herdrdev/herdr), keeps them working on your
tasks, and reports to you on Telegram. You bring:

- the agents you want to use, installed and signed in to once, by you;
- a Telegram bot token from [@BotFather](https://t.me/BotFather), for control from your
  phone. Without one, baton still runs and logs its notices.

Pick your system below. Each way ends with `baton doctor`, which checks everything and
says how to fix what is missing.

| System | Status | How |
|--------|--------|-----|
| Linux x86_64, Ubuntu | supported (the development machine, and clean installs) | [install.sh](#linux-and-macos) |
| Linux x86_64, Debian 12, Fedora 42, Arch | beta: clean install and one task pass in containers, not run on a real machine | [install.sh](#linux-and-macos) |
| Other Linux distributions | not tested (the static binary should run) | [install.sh](#linux-and-macos) |
| Linux aarch64 | beta: the binary is built by the release job, not yet run end to end by us | [install.sh](#linux-and-macos) |
| Windows 10/11 with WSL1 | supported (one machine, unattended since 2026-11-10, reboots included) | [install.sh inside WSL](#windows-with-wsl-recommended) |
| Windows 10/11 with WSL2 | beta: the same Linux path, not yet run end to end by us | [install.sh inside WSL](#windows-with-wsl-recommended) |
| Windows 10/11, native | beta | [install.ps1](#windows-native-beta) |
| macOS (Apple Silicon, Intel) | beta, not yet run end to end by us | [install.sh](#linux-and-macos) |

## What you need

- A 64-bit computer (x86_64, or ARM64 / Apple Silicon).
- About 1 GB of disk for baton, herdr and Python, plus what your agents need.
- RAM: a few hundred MB per running agent. The optional local assistant model (Ollama,
  `qwen3.5:4b`) needs about 3.5 GB more.
- `git`, and `curl` or `wget` (Linux and macOS). The script says how to get the ones you
  lack; it does not install system packages itself.
- Internet access to GitHub (herdr and baton downloads), astral.sh (uv), and Telegram.
  Behind a proxy, set `HTTPS_PROXY` (see [TROUBLESHOOTING](TROUBLESHOOTING.md#proxy)).

Nothing needs `sudo` or administrator rights. Everything goes under your home directory:

| What | Linux / macOS / WSL | Windows (native) |
|------|---------------------|------------------|
| Programs (baton, herdr, uv, baton-detect) | `~/.local/bin` | `%USERPROFILE%\.local\bin`; herdr in `%LOCALAPPDATA%\Programs\herdr` |
| Settings (`baton.toml`, `.env` with the bot token, `policy.yaml`) | `~/.config/baton` | `%USERPROFILE%\.config\baton` |
| Event log (database) | `~/.local/share/baton` | `%USERPROFILE%\.local\share\baton` |
| Logs | `~/.local/state/baton` (journald with systemd; `~/Library/Logs/baton` on macOS) | `%USERPROFILE%\.local\state\baton` |

## Linux and macOS

```bash
curl -fsSLO https://raw.githubusercontent.com/selorito/baton-herdr/v1.4.0/install.sh
less install.sh          # read what it does first: it is short
bash install.sh
```

The script:

1. installs [uv](https://docs.astral.sh/uv/) with its official installer, if it is
   missing;
2. downloads herdr 0.9.5 for your system and checks its SHA-256;
3. installs baton with uv on Python 3.12, which uv downloads if needed;
4. runs `baton setup`, which:
   - downloads `baton-detect` for your system (SHA-256 checked);
   - installs herdr's integrations for the agents it finds;
   - writes your config;
   - connects Telegram;
   - installs the service (systemd on Linux, launchd on macOS).

It asks before each step; `bash install.sh --yes` asks nothing.

**macOS:**

- Install git with `xcode-select --install` if you do not have it.
- Files downloaded by the script carry no quarantine flag, so Gatekeeper does not block
  them. A file you download with a browser does carry one; see
  [TROUBLESHOOTING](TROUBLESHOOTING.md#macos).

## Windows with WSL (recommended)

baton is proven on Windows through WSL1, and WSL2 uses the same Linux path. In PowerShell:

```powershell
wsl --install -d Ubuntu-24.04     # once; restart when asked
```

Then, inside Ubuntu, follow [Linux and macOS](#linux-and-macos). If your PC cannot run
WSL2 because virtualization is off in the BIOS, see
[WSL1](../README.md#wsl1) in the README: baton supports WSL1 too.

## Windows, native (beta)

Native Windows is new in 1.4. It passes baton's whole test suite and an end-to-end run on
Windows 11, but has not been used day to day yet. In PowerShell (not as administrator):

```powershell
Invoke-WebRequest https://raw.githubusercontent.com/selorito/baton-herdr/v1.4.0/install.ps1 -OutFile install.ps1
notepad install.ps1      # read what it does first
powershell -ExecutionPolicy Bypass -File install.ps1
```

The script:

1. checks for Git for Windows, which Claude Code needs for Git Bash and baton needs for
   projects. If it is missing, the script offers winget's per-user install;
2. installs uv;
3. installs herdr 0.9.5 into `%LOCALAPPDATA%\Programs\herdr`, SHA-256 checked, and adds it
   to your user PATH;
4. installs baton;
5. runs `baton setup`. At the end setup prints a PowerShell command that registers the
   scheduled task `baton`, which starts baton at boot. That one command needs
   administrator rights, so you run it yourself.

Open a new terminal afterwards, so that the new PATH applies.

## The agents

Install at least one, and sign in to it once yourself:

| Agent | Install | Sign in |
|-------|---------|---------|
| Claude Code | `curl -fsSL https://claude.ai/install.sh \| bash` (Windows: `irm https://claude.ai/install.ps1 \| iex`) | run `claude` |
| Codex CLI | `npm install -g @openai/codex` (needs Node.js 20+) | run `codex` |
| OpenCode | `curl -fsSL https://opencode.ai/install \| bash` | run `opencode` |

Then run `baton setup` again: it finds the new agents and installs herdr's integration
for each.

## Check, and your first task

```bash
baton doctor                       # every line ok or warn; fix every fail it names
baton task add "power function" -C ~/src/calc -i "Add power(a, b) to calc.py with a test."
baton status                       # the board
herdr --session baton              # watch the agents work
```

From your phone, write to your bot. Send `/status`, or ask in your own words, for
example "make me a one-page site for my cafe".

## Release binaries

baton itself is Python and the same on every system. Its screen reader, `baton-detect`,
is a Rust program, and baton needs it on every system: without it baton cannot read the
agents' screens (limits, questions, prompts), and `baton doctor` fails. Each release
publishes one per target, built natively on GitHub's runners because its bundled SQLite
is C:

| Asset | System | Built on | Tested by us |
|-------|--------|----------|--------------|
| `baton-detect-x86_64-unknown-linux-musl` | Linux x86_64, WSL (static, any distribution) | `ubuntu-24.04` | yes: the same musl build, made locally, in clean installs on Ubuntu, Debian, Fedora, Arch |
| `baton-detect-aarch64-unknown-linux-musl` | Linux ARM64 (static) | `ubuntu-24.04-arm` | built and run by the release job only |
| `baton-detect-aarch64-apple-darwin` | macOS, Apple Silicon | `macos-15` | built and run by the release job only |
| `baton-detect-x86_64-apple-darwin` | macOS, Intel | `macos-15-intel` | built and run by the release job only |
| `baton-detect-x86_64-pc-windows-msvc.exe` | Windows x64, native | `windows-2025` | partly: a binary built on Windows 11 for this target ran the suite and an end-to-end task there; the release job's own build has not been tried |

"Built and run by the release job" means that the job checks that the binary starts and
reports the right version on that runner. Nothing more has been run on those systems.
1.4.0 is the first release with the musl, macOS and Windows assets, so `baton setup`'s
download of them is first exercised against that release.
`baton setup` picks the asset for your system, downloads the one of baton's own version,
and checks it against the release's `SHA256SUMS`. Systems outside this table (32-bit,
Windows on ARM natively) get no binary. There `baton setup` says so, and you can build
one from a checkout with `cargo install --path crates/baton-detect --locked`.

The binaries are not code-signed. The macOS ones carry the ad-hoc signature that Apple's
linker adds, which is enough to run them when they are downloaded by `baton setup`. A copy
downloaded with a browser is quarantined; see [TROUBLESHOOTING](TROUBLESHOOTING.md#macos).

## Updating and removing

```bash
uv tool install --force --python 3.12 https://github.com/selorito/baton-herdr/releases/download/vX.Y.Z/baton_herdr-X.Y.Z-py3-none-any.whl
rm ~/.local/bin/baton-detect && baton setup      # baton-detect of the same version
baton service uninstall && uv tool uninstall baton-herdr
```

Your settings and event log stay in `~/.config/baton` and `~/.local/share/baton` until you
delete them.
