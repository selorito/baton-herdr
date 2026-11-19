---
name: baton
description: >-
  Install, check and drive baton, which keeps coding agents (Claude Code, Codex CLI,
  OpenCode) working on queued tasks inside herdr and is managed from Telegram. Use when
  the user wants to install or set up baton, run `baton doctor`, connect its Telegram
  bot, queue a task for an agent, cancel a task, change a task's tier
  (premium/economy), or fix a baton problem.
license: Apache-2.0
metadata:
  version: "1.4.0"
  requires: "baton 1.4.0 (https://github.com/selorito/baton-herdr, tag v1.4.0), herdr 0.9.1-0.9.5, git, and at least one of claude, codex, opencode, signed in by the user"
---

# baton

baton is a local orchestrator. It starts coding agents in herdr panes, resumes them after
their usage limits reset, and reports to the user on Telegram, where the user can also
give it orders. It runs as a service on the user's own machine. Everything here is done
with the `baton` CLI on that machine.

**Requires:** baton **1.4.0** from <https://github.com/selorito/baton-herdr> (tag
`v1.4.0`). Earlier versions lack the install scripts, macOS and native Windows support,
and cancelling or changing a tier in words. Check with `baton version`, which should
print `baton 1.4.0`.

## Rules for you, the agent

- **Never handle the user's secrets.** The Telegram bot token goes into `baton setup`'s
  own prompt, typed by the user, and setup stores it in `~/.config/baton/.env`.
  - Do not ask for the token in chat, put it on a command line, or print, read or
    edit `.env`.
  - The same goes for agent sign-ins and a Vercel token.
- **Make no network calls of your own.** The only download you start is the install
  script, at the pinned tag, and only when the user asks to install. Do not call the
  Telegram API, GitHub's API or anything else to "check": `baton doctor` does the
  checking.
- **Show before you run.** Before you run an install script, tell the user what it
  does. Better still, let them read it, and run it the interactive way unless they say
  otherwise.
  - Never use `sudo` or an administrator shell for baton.
  - Never change Windows execution policy permanently.
- **`baton setup` is interactive.** It asks questions and waits for the user to write to
  their bot. Ask the user to run it in their own terminal, then continue from
  `baton doctor`.
- **Report what you saw, not what you expect.** If `doctor` shows a `FAIL`, say so with
  the line. Only call baton working when `doctor` shows no `FAIL` and a task completed.

## 1. Install

First detect the system: `uname -s` (Linux / Darwin), WSL (`/proc/version` mentions
Microsoft), or native Windows (PowerShell).

**Linux, WSL, macOS:**

```bash
curl -fsSLO https://raw.githubusercontent.com/selorito/baton-herdr/v1.4.0/install.sh
less install.sh      # the user reads it; it is short
bash install.sh      # asks before each step; --yes asks nothing (only if the user says so)
```

**Windows, native** (beta; WSL is the proven way on Windows). In PowerShell, not as
administrator:

```powershell
Invoke-WebRequest https://raw.githubusercontent.com/selorito/baton-herdr/v1.4.0/install.ps1 -OutFile install.ps1
notepad install.ps1
powershell -ExecutionPolicy Bypass -File install.ps1
```

Both scripts install uv, herdr 0.9.5 (SHA-256 checked) and baton 1.4.0 under the user's
home, then run `baton setup`. They do not install the agents. The user installs at least
one and signs in once:

- Claude Code: `curl -fsSL https://claude.ai/install.sh | bash`
- Codex: `npm install -g @openai/codex`
- OpenCode: `curl -fsSL https://opencode.ai/install | bash`

After the user installs a new agent, have them run `baton setup` again: it installs
herdr's integration for it.

On native Windows, setup ends by printing one PowerShell command that registers the
`baton` scheduled task. That command needs administrator rights, so the user runs it
themselves. On WSL1 it is the same.

## 2. Check: `baton doctor`

```bash
baton doctor
```

Each line is `ok`, `warn` or `FAIL`, followed by `-> what to do` when it is not ok.
Work through the `FAIL`s first, one at a time, doing what the hint says, and run
`doctor` again after each fix. The common ones:

| Line | Usual fix |
|------|-----------|
| `herdr server not reachable` | Linux/macOS: run `baton setup` again (its last step installs and starts the service; `baton service install` alone only writes the files). WSL1/Windows: start the scheduled task |
| `baton-detect ... not found` or version mismatch | `rm ~/.local/bin/baton-detect && baton setup` |
| `<agent> ... is not on PATH` | Install that agent, or remove it from `[scheduler] agents` |
| `<agent> integration` | `herdr integration install <agent>` |
| `telegram ...` | See section 3 |

`baton service status` says whether herdr's server and batond are running. Logs:

- `journalctl --user -u baton.service` on Linux;
- `~/Library/Logs/baton/` on macOS;
- `~/.local/state/baton/` on WSL1 and native Windows.

## 3. Connect Telegram

Telegram is how the user manages baton from their phone. The user does these steps
themselves:

1. Create a bot with [@BotFather](https://t.me/BotFather) (`/newbot`) and copy its token.
2. Run `baton setup` in their terminal and paste the token when asked. Setup then waits
   for them to write to the bot, finds their chat and user id itself, and writes
   `[telegram] chat_id` and `owner_id` to `~/.config/baton/baton.toml`.
3. batond has to restart to pick it up.
   - **Linux (systemd), macOS:** setup's last step does that when the user answers yes
     to "Update it?".
   - **WSL1, native Windows:** in PowerShell, run
     `Stop-ScheduledTask -TaskName baton; Start-ScheduledTask -TaskName baton`.

Then `baton doctor` should show `ok telegram @<bot>, actions from user <id>`.

- **`notices only`:** `owner_id` is missing. The bot reports, but takes no orders.
- **`bot not reachable`:** a wrong token, or a network that needs a proxy. Set
  `HTTPS_PROXY`, or this in `baton.toml`:
  ```toml
  [telegram]
  proxy = "http://host:port"
  ```

One bot per baton: two batonds with one token take each other's messages.

What the user can do from Telegram:

- `/new <request>`: a project, planned by an agent. A plan comes back with Start and
  Cancel. Add `#economy` to run it on the cheaper model.
- `/status`, `/budget`: open tasks, and what each agent has left.
- `/cancel t-1a2b3c4d`: drop a task or project and close its pane.
- `/tier t-1a2b3c4d economy` (or `premium`): the tier from its next attempt.
- Approve or Deny under a permission prompt. Reply to a question to answer it.
- `dur`, `iptal` or `stop` (alone, or with `hepsini` / `all`, or with an id) stops the
  work under way at once.
- With the optional local assistant (Ollama) on, plain sentences work too. For example
  "cancel the cafe task" or "run t-1a2b3c4d on economy". Each one comes back as a card
  whose button does the same as the command.

## 4. Queue a task

```bash
baton task add "power function" -C ~/src/calc -i "Add power(a, b) to calc.py with a test."
baton task add "refactor" -C ~/src/app -f instructions.md --tier economy
baton status                 # every task and its attempts; ids look like t-1a2b3c4d
baton budget                 # what each agent has left, and where a new task would go
```

- `-C` is the working directory: an existing directory, usually a git repository. The
  agent works there.
- Write instructions as a complete prompt for a coding agent: what to change, where,
  and how to tell it is done (tests).
- Without `--tier`, baton's roles decide. `premium` is the usual model, `economy` the
  cheaper one.
- batond (the service) picks the agent with quota left and starts it. Without the
  service, `baton run` runs open tasks once and exits.
- To watch, run `herdr --session baton`. Do not type into an agent's pane while it is
  working.
- When an agent asks for permission, the user answers in Telegram, or runs
  `baton approve t-…`, `baton deny t-…` or `baton answer t-… "<text>"`.

## 5. Cancel a task, change its tier

```bash
baton task cancel t-1a2b3c4d --reason "not needed"   # closes its agent's pane
baton task tier t-1a2b3c4d economy                    # from the next attempt
```

Confirm the id with `baton status` first. Cancelling cannot be undone: the task is
dropped. To redo the work, queue a new task.

## 6. When something goes wrong

Read the troubleshooting guide for the user's system, then match `baton doctor`'s output
against it. There are three copies:

- `TROUBLESHOOTING.md`, next to this file in the skill (no download needed);
- `docs/TROUBLESHOOTING.md` in a checkout;
- <https://github.com/selorito/baton-herdr/blob/v1.4.0/docs/TROUBLESHOOTING.md>.

It covers, per system:

- the herdr version range;
- baton-detect;
- a `baton.toml` that does not parse (including one saved with a byte order mark);
- a taken preview port, proxies, and Telegram;
- on WSL1, panes closing on long prompts, the scheduled task, and log size;
- macOS Gatekeeper;
- Windows paths and PATH.

Support status for 1.4.0:

- **Supported:** Ubuntu on x86_64 and Windows through WSL1. Both were run on real
  machines.
- **Beta:**
  - Debian 12, Fedora 42 and Arch: tested in containers only.
  - Native Windows: a real machine, but no real agent yet.
  - macOS (Apple Silicon and Intel), Linux ARM64 and WSL2: not run by the maintainers.

On a beta system, tell the user that before you start.
