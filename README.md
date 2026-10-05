# baton-herdr

[![CI](https://github.com/selorito/baton-herdr/actions/workflows/ci.yml/badge.svg)](https://github.com/selorito/baton-herdr/actions/workflows/ci.yml)

baton-herdr (`baton` for short) is an orchestrator for coding agents (Claude Code, Codex CLI and OpenCode in v1)
running inside [herdr](https://github.com/herdrdev/herdr). It watches each agent through
herdr's socket API and plugin system, resumes work an agent left unfinished, and assigns new
tasks based on how much token quota each agent has left. It is one Python process (batond)
with a CLI and a Telegram bot. Every change of state is recorded in an append-only event log.

**Status: MVP.** baton runs unattended on one Linux machine: it starts tasks, hands them
over when an agent hits its usage limit, resumes crashed sessions, survives its own
restarts, and asks you (CLI or Telegram) when an agent needs a decision. Quota-aware
scheduling is next; see [docs/ROADMAP.md](docs/ROADMAP.md).

## Your responsibility

baton drives the official, unmodified agent CLIs in a terminal, the way you would, and never
touches their credentials ([ADR 0005](docs/adr/0005-automation-boundaries.md)). Your use of
each agent is still governed by its vendor's terms; read them before you let baton run
unattended: [Anthropic](https://code.claude.com/docs/en/legal-and-compliance),
[OpenAI](https://openai.com/policies/row-terms-of-use/). Notes and sources:
[docs/research/agents.md](docs/research/agents.md).

## Install

You need Linux, [uv](https://docs.astral.sh/uv/), a Rust toolchain
([rustup](https://rustup.rs/)), [herdr](https://github.com/herdrdev/herdr) 0.9.1 or later,
and the agents you want to use (Claude Code, Codex CLI, OpenCode), each signed in once by
you.

```bash
git clone https://github.com/selorito/baton-herdr.git && cd baton-herdr
uv tool install .                         # puts `baton` in ~/.local/bin
cargo install --path crates/baton-detect --locked   # `baton-detect` in ~/.cargo/bin
herdr integration install claude          # and codex / opencode: baton needs the session
herdr integration install codex           # ids these integrations report
mkdir -p ~/.config/baton
cp baton.example.toml ~/.config/baton/baton.toml
```

Edit `~/.config/baton/baton.toml`: at least `[scheduler] agents` (in order of preference)
and `timezone` (agents print limit reset times in local time). baton reads
`./baton.toml` if there is one, otherwise `~/.config/baton/baton.toml`; `BATON_CONFIG`
overrides both. Secrets go in `~/.config/baton/.env` (see `.env.example`).

`baton-detect` is the usage collector batond runs ([ADR 0011](docs/adr/0011-baton-detect-in-rust.md)):
it follows the agents' own logs (Claude Code and Codex transcripts, OpenCode's database)
and reports the tokens each response used and Codex's rate-limit windows. batond keeps them
in their own tables, apart from the task log. `baton-detect usage --once` prints what it
finds, one JSON object per line (`schemas/usage-event.v1.json`).

Checked on real logs (one Linux machine, 2026-10-03, read-only). One `--once` pass over every
session took 0.66 s and produced:

| Source | Records |
|--------|--------:|
| Claude Code responses | 1,809 |
| Codex responses | 4,074 |
| OpenCode messages | 16 |
| Codex rate-limit observations | 1,063 |

- No `record_id` appeared twice.
- OpenCode's records add up exactly to the token totals OpenCode keeps per session.
- Codex's per-response deltas, worked out from its running totals, match the per-response
  figures Codex writes itself.
- Two Claude Code lines were not valid JSON (Python's parser rejects them as well); they are
  reported and skipped.

Then check everything:

```bash
baton doctor
```

It checks the config, the database, herdr and its integrations, the agents on `PATH`, the
timezone and Telegram, and says how to fix what is missing.

## Run

As a systemd user service (recommended):

```bash
baton service install          # writes baton-herdr.service and baton.service; enables nothing
systemctl --user daemon-reload
systemctl --user enable --now baton.service
loginctl enable-linger "$USER" # keep running after you log out
```

`baton-herdr.service` runs a herdr server in the session named by `[herdr] session` (default
`baton`, the same session the CLI uses), started with a
clean environment; `baton.service` runs `baton daemon` against it. Watch the agents with
`herdr --session baton`, and batond's log with `journalctl --user -u baton -f`.

In the foreground instead, start a herdr server from a plain terminal (not from inside an
agent, whose environment the agents would inherit), then the daemon:

```bash
herdr --session baton server &
baton daemon                                 # Ctrl+C stops it
```

## Use

```bash
baton task add "power function" -C ~/src/calc \
  -i "Add power(a, b) to calc.py with a test."
baton status
```

The daemon picks the task up within 30 seconds, starts the first available agent in a pane of
its own workspace, sends the task, and follows the agent until the turn ends. What it does
on its own:

- **Usage limit:** the task moves to the next available agent, with a note that earlier work
  is in the directory; if every agent is limited, it waits for the reset and resumes the same
  session.
- **Crash or stall:** the agent's own session is resumed (twice per task by default); a
  session that cannot be reopened is restarted fresh.
- **batond restart:** active attempts are picked up where the event log says they were;
  nothing is sent twice.
- **End of a turn:** every prompt asks the agent to end with `[[BATON:END status=done]]`,
  `question` or `blocked`. A question or a blocker comes to you instead of closing the task.

### Budget

```bash
baton budget
```

```
claude: ~61% left (estimate against [budget] claude_window_tokens) · 5h window 1.2M of ~3.1M tokens, resets ~16:40
codex: 99% left · 5h 0% used · weekly 1% used, resets 23:37

Next task: claude: first in preference order, ~61% left (estimate).
```

Codex's figures are its own (`rate_limits` in its logs). Claude Code does not report its
limits, so baton estimates its 5-hour session window from the tokens it recorded. The cap
comes from `[budget] claude_window_tokens`, or from the last session limit baton saw. With
neither, Claude's budget is `unknown`. Estimates are always marked as such.

The scheduler keeps your `[scheduler] agents` order. An agent with less than
`[budget] reserve_percent` (default 10) left goes behind the others. A low budget never
stops a task; only a limit the agent actually hits does. Every choice is recorded in the
event log as `task.agent_chosen`, with its reason. See
[ADR 0012](docs/adr/0012-remaining-budget.md).

### When an agent needs you

You get a notice: a permission prompt (with what it asks for), a question, or a blocker.
Answer at the terminal (`herdr --session baton`), or:

```bash
baton approve <task>           # a permission prompt (Claude Code, Codex)
baton deny <task>              # the agent then asks what to do instead
baton answer <task> "Use English."
```

Remote actions follow [ADR 0009](docs/adr/0009-remote-operator-actions.md): each one applies
only to the prompt you were shown, at most once, and only after baton has verified the pane
still hosts that agent session. Folder trust, hook review and sign-in are always answered at
the terminal.

### Telegram

1. Create a bot with [@BotFather](https://t.me/BotFather) and put its token in
   `~/.config/baton/.env`: `BATON_TELEGRAM__BOT_TOKEN=...`
2. Send your bot any message, then open `https://api.telegram.org/bot<token>/getUpdates`.
   `message.chat.id` is your `chat_id`; `message.from.id` is your `owner_id` (in a private
   chat they are the same number).
3. Set both under `[telegram]` and restart: `systemctl --user restart baton`.

Notices then arrive in that chat, each headed by the task's title and ending with its id:

- **Started:** the agent, and why the scheduler chose it.
- **Moved:** from which agent to which, and why. For a usage limit, also when it lifts, in
  your `[scheduler] timezone`.
- **Needs you, or stopped:** what is asked or what went wrong, with the last lines of the
  agent's screen.
- **Done:** how long the task took, the tokens its last attempt used, the files it changed
  (like `git diff --stat`, at most five), and that agent's budget left. An estimate is marked
  as one.

Known secret shapes (API keys, tokens, private keys, `password=…`) are masked before
anything is sent.

Permission prompts carry Approve / Deny buttons. Reply to a question to answer it. `/status`
lists open tasks and agents, and `/budget` what each agent has left. Messages from anyone
else, or from another chat, are ignored. Without `owner_id` the bot only sends notices.

### Operate

- Data: the event log at `~/.local/share/baton/baton.db` (`[database] path`). Every state
  change is in it; `baton status` is a view of it.
- Upgrade: `git pull && uv tool install --force . && cargo install --path crates/baton-detect
  --locked && systemctl --user restart baton`.
- Screen classifier: the Python rules by default. `[detector] engine = "shadow"` also runs
  the Rust classifier (`baton-detect classify`, [ADR 0011](docs/adr/0011-baton-detect-in-rust.md))
  on every screen and logs where the two differ:
  `journalctl --user -u baton | grep "detector mismatch"`. `engine = "rust"` lets the Rust
  one decide, with Python as the fallback.
- Stop: `systemctl --user stop baton baton-herdr` (agents in the baton session stop with
  their herdr server).

## Results

`just bench` runs baton's own loop, scheduler, detection and recovery against simulated
agents, on simulated time. 50 one-task worlds run per scenario, seed 1
([full results](bench/results/2026-10-05.md); the model and its limits are in
[bench/README.md](bench/README.md)).

| Scenario | Done without a person | Stop → work goes on (median) | Steps redone per stop |
|---|---:|---:|---:|
| Usage limit mid-task, another agent free | 100 % | 6.0 s | 1.00 |
| Crash, 1–3 times | 86 % (the rest: a third crash, sent to you by policy) | 4.7 s | 1.00 |
| Hang (screen says "working", nothing moves) | 100 % | 53 min | 1.00 |
| Both agents hit limits in turn | 100 % | 11.6 min | 0.94 |
| Every agent limited, wait for the reset | 100 % | 26 min (work resumes 35 s after the reset) | 0.50 |

- A limit is acted on as soon as herdr reports the screen. baton's own detection delay is
  zero in the simulation; herdr's is not modelled.
- A hang is found only when the turn timeout runs out (`turn_timeout_seconds`, default
  1 h). That is the weakest case.
- Budget-aware choice was measured on 30 queues of ten tasks, with Claude's window
  40–80 % used:
  - with `claude_window_tokens` set, mid-task limits dropped from 30 to 7, and counted
    tokens fell by 1.9 %;
  - with a 20 % reserve, limits dropped to 0 and tokens fell by 3.5 %;
  - learning the cap from a limit alone changed nothing within one window.

## Development

Requirements: [uv](https://docs.astral.sh/uv/), a stable Rust toolchain (via rustup) and
[just](https://just.systems/).

```bash
uv sync                  # create .venv with Python 3.12 and all dependencies
just check               # lint, type-check, import contracts and tests (Python and Rust)
just fmt                 # format everything
just test                # run tests only
just smoke               # optional: live test against an installed herdr

uv run baton version
cargo run -p baton-detect -- --version
```

`just smoke` needs the `herdr` binary on `PATH`. It starts its own headless herdr server in
a throwaway named session (its own socket), drives a shell pane, then stops the server and
deletes the session. It never touches a herdr session you have open, starts no agent, and is
not part of `just check` or CI.

Configuration for development: a `baton.toml` and `.env` in the checkout are used before the
ones in `~/.config/baton/`. Any setting can be overridden with `BATON_<SECTION>__<KEY>`.

Architecture decisions are in [docs/adr/](docs/adr/). Contributor and agent guidelines are in
[CLAUDE.md](CLAUDE.md).

## License

Apache-2.0. See [LICENSE](LICENSE).
