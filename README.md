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

You need Linux, [uv](https://docs.astral.sh/uv/), [herdr](https://github.com/herdrdev/herdr)
0.9.1 or later, and the agents you want to use (Claude Code, Codex CLI, OpenCode), each
signed in once by you.

```bash
git clone https://github.com/selorito/baton-herdr.git && cd baton-herdr
uv tool install .                         # puts `baton` in ~/.local/bin
herdr integration install claude          # and codex / opencode: baton needs the session
herdr integration install codex           # ids these integrations report
mkdir -p ~/.config/baton
cp baton.example.toml ~/.config/baton/baton.toml
```

Edit `~/.config/baton/baton.toml`: at least `[scheduler] agents` (in order of preference)
and `timezone` (agents print limit reset times in local time). baton reads
`./baton.toml` if there is one, otherwise `~/.config/baton/baton.toml`; `BATON_CONFIG`
overrides both. Secrets go in `~/.config/baton/.env` (see `.env.example`).

Then check everything:

```bash
baton doctor
```

It checks the config, the database, herdr and its integrations, the agents on `PATH`, the
timezone and Telegram, and says how to fix what is missing.

## Run

As a systemd user service (recommended):

```bash
baton service install          # writes baton-herdr.service and baton_herdr.service; enables nothing
systemctl --user daemon-reload
systemctl --user enable --now baton_herdr.service
loginctl enable-linger "$USER" # keep running after you log out
```

`baton-herdr.service` runs a herdr server in a session of its own (`baton`), started with a
clean environment; `baton_herdr.service` runs `baton daemon` against it. Watch the agents with
`herdr --session baton`, and batond's log with `journalctl --user -u baton -f`.

In the foreground instead, start a herdr server from a plain terminal (not from inside an
agent, whose environment the agents would inherit), then the daemon:

```bash
herdr --session baton server &
BATON_HERDR__SESSION=baton baton daemon      # Ctrl+C stops it
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

Notices then arrive in that chat. Permission prompts carry Approve / Deny buttons; reply to a
question to answer it; `/status` lists open tasks and agents. Messages from anyone else, or
from another chat, are ignored. Without `owner_id` the bot only sends notices.

### Operate

- Data: the event log at `~/.local/share/baton/baton.db` (`[database] path`). Every state
  change is in it; `baton status` is a view of it.
- Upgrade: `git pull && uv tool install --force . && systemctl --user restart baton`.
- Stop: `systemctl --user stop baton baton-herdr` (agents in the baton session stop with
  their herdr server).

## Moving from an earlier install

The project was renamed ([ADR 0010](docs/adr/0010-project-name.md) gives the old name). If
you installed it under the old name, set `old` to that name and run these steps; nothing old
is removed until you remove it.

```bash
old=...   # the previous name, from ADR 0010

# 1. Stop the old services (if you installed them) and remove their unit files.
systemctl --user disable --now "$old.service" "$old-herdr.service"
rm ~/.config/systemd/user/"$old.service" ~/.config/systemd/user/"$old-herdr.service"
systemctl --user daemon-reload

# 2. Replace the CLI (from this checkout).
uv tool uninstall "$old"
uv tool install .

# 3. Settings: new directory, file name and environment prefix.
mv ~/.config/"$old" ~/.config/baton
mv ~/.config/baton/"$old.toml" ~/.config/baton/baton.toml
sed -i "s/^${old^^}_/BATON_/" ~/.config/baton/.env

# 4. The event log.
mv ~/.local/share/"$old" ~/.local/share/baton
mv ~/.local/share/baton/"$old.db" ~/.local/share/baton/baton.db

# 5. Check, then install and start the new services.
baton doctor
baton service install && systemctl --user daemon-reload
systemctl --user enable --now baton.service
```

The service now runs its herdr server in the session `baton`. Attempts still running in the
old session are not found there: they count as crashed and are resumed in the new session.
A question or permission prompt that was already waiting before the move is answered at the
terminal; remote actions apply to prompts recorded after it.

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
