# coban

coban is an orchestrator for coding agents (Claude Code, Codex CLI and OpenCode in v1)
running inside [herdr](https://github.com/herdrdev/herdr). It watches each agent through
herdr's socket API and plugin system, resumes work an agent left unfinished, and assigns new
tasks based on how much token quota each agent has left. It is one Python process (cobanD)
with a CLI and a Telegram bot. Every change of state is recorded in an append-only event log.

**Status: MVP.** coban runs unattended on one Linux machine: it starts tasks, hands them
over when an agent hits its usage limit, resumes crashed sessions, survives its own
restarts, and asks you (CLI or Telegram) when an agent needs a decision. Quota-aware
scheduling is next; see [docs/ROADMAP.md](docs/ROADMAP.md).

## Your responsibility

coban drives the official, unmodified agent CLIs in a terminal, the way you would, and never
touches their credentials ([ADR 0005](docs/adr/0005-automation-boundaries.md)). Your use of
each agent is still governed by its vendor's terms; read them before you let coban run
unattended: [Anthropic](https://code.claude.com/docs/en/legal-and-compliance),
[OpenAI](https://openai.com/policies/row-terms-of-use/). Notes and sources:
[docs/research/agents.md](docs/research/agents.md).

## Install

You need Linux, [uv](https://docs.astral.sh/uv/), [herdr](https://github.com/herdrdev/herdr)
0.9.1 or later, and the agents you want to use (Claude Code, Codex CLI, OpenCode), each
signed in once by you.

```bash
git clone <this repository> coban && cd coban
uv tool install .                         # puts `coban` in ~/.local/bin
herdr integration install claude          # and codex / opencode: coban needs the session
herdr integration install codex           # ids these integrations report
mkdir -p ~/.config/coban
cp coban.example.toml ~/.config/coban/coban.toml
```

Edit `~/.config/coban/coban.toml`: at least `[scheduler] agents` (in order of preference)
and `timezone` (agents print limit reset times in local time). coban reads
`./coban.toml` if there is one, otherwise `~/.config/coban/coban.toml`; `COBAN_CONFIG`
overrides both. Secrets go in `~/.config/coban/.env` (see `.env.example`).

Then check everything:

```bash
coban doctor
```

It checks the config, the database, herdr and its integrations, the agents on `PATH`, the
timezone and Telegram, and says how to fix what is missing.

## Run

As a systemd user service (recommended):

```bash
coban service install          # writes coban-herdr.service and coban.service; enables nothing
systemctl --user daemon-reload
systemctl --user enable --now coban.service
loginctl enable-linger "$USER" # keep running after you log out
```

`coban-herdr.service` runs a herdr server in a session of its own (`coban`), started with a
clean environment; `coban.service` runs `coban daemon` against it. Watch the agents with
`herdr --session coban`, and cobanD's log with `journalctl --user -u coban -f`.

In the foreground instead, start a herdr server from a plain terminal (not from inside an
agent, whose environment the agents would inherit), then the daemon:

```bash
herdr --session coban server &
COBAN_HERDR__SESSION=coban coban daemon      # Ctrl+C stops it
```

## Use

```bash
coban task add "power function" -C ~/src/calc \
  -i "Add power(a, b) to calc.py with a test."
coban status
```

The daemon picks the task up within 30 seconds, starts the first available agent in a pane of
its own workspace, sends the task, and follows the agent until the turn ends. What it does
on its own:

- **Usage limit:** the task moves to the next available agent, with a note that earlier work
  is in the directory; if every agent is limited, it waits for the reset and resumes the same
  session.
- **Crash or stall:** the agent's own session is resumed (twice per task by default); a
  session that cannot be reopened is restarted fresh.
- **cobanD restart:** active attempts are picked up where the event log says they were;
  nothing is sent twice.
- **End of a turn:** every prompt asks the agent to end with `[[COBAN:END status=done]]`,
  `question` or `blocked`. A question or a blocker comes to you instead of closing the task.

### When an agent needs you

You get a notice: a permission prompt (with what it asks for), a question, or a blocker.
Answer at the terminal (`herdr --session coban`), or:

```bash
coban approve <task>           # a permission prompt (Claude Code, Codex)
coban deny <task>              # the agent then asks what to do instead
coban answer <task> "Use English."
```

Remote actions follow [ADR 0009](docs/adr/0009-remote-operator-actions.md): each one applies
only to the prompt you were shown, at most once, and only after coban has verified the pane
still hosts that agent session. Folder trust, hook review and sign-in are always answered at
the terminal.

### Telegram

1. Create a bot with [@BotFather](https://t.me/BotFather) and put its token in
   `~/.config/coban/.env`: `COBAN_TELEGRAM__BOT_TOKEN=...`
2. Send your bot any message, then open `https://api.telegram.org/bot<token>/getUpdates`.
   `message.chat.id` is your `chat_id`; `message.from.id` is your `owner_id` (in a private
   chat they are the same number).
3. Set both under `[telegram]` and restart: `systemctl --user restart coban`.

Notices then arrive in that chat. Permission prompts carry Approve / Deny buttons; reply to a
question to answer it; `/status` lists open tasks and agents. Messages from anyone else, or
from another chat, are ignored. Without `owner_id` the bot only sends notices.

### Operate

- Data: the event log at `~/.local/share/coban/coban.db` (`[database] path`). Every state
  change is in it; `coban status` is a view of it.
- Upgrade: `git pull && uv tool install --force . && systemctl --user restart coban`.
- Stop: `systemctl --user stop coban coban-herdr` (agents in the coban session stop with
  their herdr server).

## Development

Requirements: [uv](https://docs.astral.sh/uv/), a stable Rust toolchain (via rustup) and
[just](https://just.systems/).

```bash
uv sync                  # create .venv with Python 3.12 and all dependencies
just check               # lint, type-check, import contracts and tests (Python and Rust)
just fmt                 # format everything
just test                # run tests only
just smoke               # optional: live test against an installed herdr

uv run coban version
cargo run -p coban-detect -- --version
```

`just smoke` needs the `herdr` binary on `PATH`. It starts its own headless herdr server in
a throwaway named session (its own socket), drives a shell pane, then stops the server and
deletes the session. It never touches a herdr session you have open, starts no agent, and is
not part of `just check` or CI.

Configuration for development: a `coban.toml` and `.env` in the checkout are used before the
ones in `~/.config/coban/`. Any setting can be overridden with `COBAN_<SECTION>__<KEY>`.

Architecture decisions are in [docs/adr/](docs/adr/). Contributor and agent guidelines are in
[CLAUDE.md](CLAUDE.md).

## License

Apache-2.0. See [LICENSE](LICENSE).
