# baton-herdr

[![CI](https://github.com/selorito/baton-herdr/actions/workflows/ci.yml/badge.svg)](https://github.com/selorito/baton-herdr/actions/workflows/ci.yml)
[![License: Apache-2.0](https://img.shields.io/badge/license-Apache--2.0-blue.svg)](LICENSE)
[![Python 3.12+](https://img.shields.io/badge/python-3.12%2B-blue.svg)](pyproject.toml)
[![Rust 1.98+](https://img.shields.io/badge/rust-1.98%2B-orange.svg)](Cargo.toml)

baton keeps a queue of coding tasks moving across Claude Code, Codex CLI and OpenCode running
in [herdr](https://github.com/herdrdev/herdr). When one agent hits its usage limit, crashes or
stalls, baton resumes the task or hands it to another agent, and tells you on Telegram.

![baton handing a task from Claude Code to Codex in herdr](docs/assets/demo.gif)

Simulated agents; reproduce with `just demo`.

<!-- Real run, recorded on a phone: Telegram notices for a hand-off and a finished task.
     Add it as docs/assets/telegram.gif (or .mp4 linked from here) when it exists. -->

## Why

Subscription coding agents stop when their usage window runs out, and a long task left
unattended stops with them. Moving the task by hand means noticing it first, then telling
the next agent what was done.

Other herdr tools cover parts of this:
- [claude-auto-retry-herdr](https://github.com/kil9/claude-auto-retry-herdr) waits for
  Claude's reset and retries the same session;
- [herdr-codex-handoff](https://github.com/TeXmeijin/herdr-codex-handoff) continues a task
  in a new Codex pane when you press a key;
- [herdr-supervisor](https://github.com/hao1939/herdr-supervisor) has an LLM agent decide
  how to help workers reach their goals.

baton does the recovery across agents on its own. Its decisions are plain, tested code,
and each one is recorded with its reason in an event log.

## Quickstart

On Linux, with [uv](https://docs.astral.sh/uv/), [rustup](https://rustup.rs/),
[herdr](https://github.com/herdrdev/herdr) ≥ 0.9.1, and the agents installed and signed in
once by you:

```bash
git clone https://github.com/selorito/baton-herdr.git && cd baton-herdr && uv tool install . && cargo install --path crates/baton-detect --locked
herdr integration install claude && herdr integration install codex
mkdir -p ~/.config/baton && cp baton.example.toml ~/.config/baton/baton.toml   # set agents and timezone
baton service install && systemctl --user daemon-reload && systemctl --user enable --now baton.service
baton task add "power function" -C ~/src/calc -i "Add power(a, b) to calc.py with a test."
```

`baton status` shows the board, `herdr --session baton` shows the agents at work, and
`baton doctor` says what is missing and how to fix it. More detail:
[Install](#install), [Operate](#operate).

## How it works

batond is one Python process. It opens panes in herdr and types into them through herdr's
socket API, the way you would. It reads each agent's screen to know what the agent is
doing:
- working;
- waiting for a decision;
- limited until a given time;
- gone.

Every observation and decision is appended to an event log first, then acted on.
Afterwards, the log answers "why did this task move to Codex at 14:02?".

On a usage limit, the task goes to the next available agent, with a note that earlier work
is in the directory. If every agent is limited, it waits for the first reset and resumes the
same session. A crash or a stall resumes the agent's own session, twice per task by default.

**[docs/ARCHITECTURE.md](docs/ARCHITECTURE.md)** follows one task from `task add` to the
done notice, with the file and function behind each step.

## Results

`just bench` runs baton's own loop, scheduler, detection and recovery against simulated
agents, on simulated time. It uses 50 one-task worlds per scenario and seed 1
([full results](bench/results/2026-10-05-2.md); the model and its limits are in
[bench/README.md](bench/README.md)).

| Scenario | Done without a person | Stop → work goes on (median) | Steps redone per stop |
|---|---:|---:|---:|
| Usage limit mid-task, another agent free | 100 % | 6.0 s | 1.00 |
| Crash, 1–3 times | 86 % (the rest: a third crash, sent to you by policy) | 4.7 s | 1.00 |
| Hang (screen says "working", nothing moves) | 100 % | 14.5 min (was 53 min) | 1.00 |
| Hang, usage not collected | 100 % | 29.5 min | 1.00 |
| Long response (5–10 min, still screen, not a hang) | 100 %, no false alarm | – | – |
| Both agents hit limits in turn | 100 % | 11.6 min | 0.94 |
| Every agent limited, wait for the reset | 100 % | 26 min (work resumes 35 s after the reset) | 0.50 |

- **Limits** are acted on as soon as herdr reports the screen. baton's own delay is zero in
  the simulation; herdr's is not modelled.
- **A hang** is a working agent whose screen (counters and spinners aside) and recorded
  tokens both stay still for 15 minutes ([ADR 0014](docs/adr/0014-stall-detection.md)).
  That is longer than either agent's own request timeout. Before this rule, only the
  one-hour turn timeout caught hangs ([earlier results](bench/results/2026-10-05.md)).
- **Budget-aware choice**, measured on 30 queues of ten tasks with Claude's window 40–80 %
  used:
  - with `claude_window_tokens` set, mid-task limits dropped from 30 to 7, and counted
    tokens fell by 1.9 %;
  - with a 20 % reserve, limits dropped to 0, and tokens fell by 3.5 %;
  - learning the cap from a limit alone changed nothing within one window.

These are baton's mechanics under a stated model, not measurements of real agents.

## Telegram

1. Create a bot with [@BotFather](https://t.me/BotFather) and put its token in
   `~/.config/baton/.env`: `BATON_TELEGRAM__BOT_TOKEN=...`
2. Send the bot a message. Then open `https://api.telegram.org/bot<token>/getUpdates`:
   - `message.chat.id` is your `chat_id`;
   - `message.from.id` is your `owner_id`.
3. Set both under `[telegram]`, then run `systemctl --user restart baton`.

Each notice starts with the task's title and ends with its id:

- **Started**: the agent, and why the scheduler chose it.
- **Moved**: from which agent to which, and why. For a usage limit, also when it lifts, in
  your time zone.
- **Needs you / stopped**: the question or the error, with the last lines of the agent's
  screen.
- **Done**: how long the task took, the tokens its attempt used, the files it changed (at
  most five), and the agent's budget left.

Permission prompts carry Approve / Deny buttons. To answer a question, reply to it; a
message that is not a reply reaches no agent, and the bot says so.

Only what needs you makes a sound: permission prompts, questions, and every agent being
limited. The other notices arrive silently.
`/status` lists open tasks and agents, and `/budget` shows what each agent has left.

Two safeguards:
- Messages from anyone but `owner_id`, or from another chat, are ignored.
- Anything shaped like a secret is masked before it is sent: API keys, tokens, private
  keys, `password=…`, and the bot's own token.

The same actions work from the CLI: `baton approve|deny <task>`,
`baton answer <task> "…"`. Each one applies only to the prompt you were shown, at most
once, and only after baton has verified the pane still hosts that agent session
([ADR 0009](docs/adr/0009-remote-operator-actions.md)).

## Budget

```
$ baton budget
claude: ~61% left (estimate against [budget] claude_window_tokens) · 5h window 1.2M of ~3.1M tokens, resets ~16:40
codex: 99% left · 5h 0% used · weekly 1% used, resets 23:37

Next task: claude: first in preference order, ~61% left (estimate).
```

- **Codex** reports its own rate-limit windows in its logs.
- **Claude Code** does not report its limits, so baton estimates its five-hour window from
  the tokens it recorded. The cap comes from `[budget] claude_window_tokens`, or from the
  last limit baton saw. Estimates are always marked as such.

The scheduler keeps your preference order. An agent with less than `reserve_percent`
(default 10) left goes behind the others. A low budget never stops a task; only a limit the
agent actually hits does. Every choice is logged as `task.agent_chosen`, with its reason
([ADR 0012](docs/adr/0012-remaining-budget.md)).

## Policy

Agents ask before running shell commands. baton answers the harmless ones itself and sends
you the rest ([ADR 0013](docs/adr/0013-permission-policy.md)).

- **Allowed** by default: only commands that read (`ls`, `cat`, `grep`, `git status`,
  `git diff`, …).
- **Allowed only in your `trusted_dirs`**: tests and builds (`pytest`, `cargo test`,
  `npm test`, `make`, …). They run the project's own code, which can do anything, so
  whether to trust it is your call, made once per repository. Elsewhere they ask.
- **Asked** by default:
  - deleting (`rm`), pushing (`git push`), forcing (`--force`);
  - history rewrites, migrations, `sudo`;
  - downloads (`curl`, `wget`, package installs, `git clone`);
  - anything no rule matches.
- **Your rules and `trusted_dirs`** go in `~/.config/baton/policy.yaml`
  ([example](policy.example.yaml)). Rules are tried first and may `allow`, `ask` or
  `deny` by shell pattern or regex, per agent.

A command is split where the shell would split it, and the strictest part wins, so
`pytest && git push` asks. These always ask:
- command substitution;
- redirection to a file;
- anything baton cannot read with certainty.

```
$ baton policy check "pytest -q" --dir ~/src/other
ask: pytest -q: runs the project's own code (tests, builds), so it is allowed only in trusted_dirs, and /home/you/src/other is not one
```

Each decision is logged as `attempt.permission_decided`, with the rule and the reason.
Upgrading from 0.1, where every prompt came to you: `baton doctor` warns until a
`policy.yaml` exists, and `[policy] enabled = false` restores the old behaviour. In v1 the
policy reads Claude Code's Bash prompts. Codex command approvals, file edits and
OpenCode prompts still come to you.

## Limitations and terms

- **Linux only.** One machine, one task at a time.
- **Screen reading.** baton reads agents' screens. A new agent version can change a screen
  baton relies on; the rules are tested against recorded screens in `fixtures/`, so a
  change shows up as a failing test once recorded, not before.
- **Hangs** take 15 minutes to catch (30 without usage collection): long enough not to
  mistake a slow response for one.
- **Claude's budget** is an estimate. Codex's is its own figure.
- **baton-detect must run.** Without it, batond keeps herdr's working and blocked states but
  finishes and hands off nothing until it is back. `baton doctor` checks it.
- **Codex's update chooser** (Codex ≥ 0.156) is not recognised by herdr 0.9.1
  ([herdr#4811](https://github.com/herdrdev/herdr/issues/4811)). baton can then type the
  task into it, which picks "Update now" and runs Codex's own update. Set
  `check_for_update_on_startup = false` in `~/.codex/config.toml` for unattended use.
- **Telegram** was tested live with a phone (2026-10-06) against fake agents: buttons,
  replies, hand-off, stall, `/status`, `/budget`, sounds, and updates from anyone but
  `owner_id` ignored. Real agents' screens were not part of that run.

baton drives the official, unmodified agent CLIs in a terminal, and never touches their
credentials ([ADR 0005](docs/adr/0005-automation-boundaries.md)). It does not rotate
accounts or evade limits: it waits for them or moves on. Your use of each agent is still
governed by its vendor's terms; read them before you let baton run unattended:
[Anthropic](https://code.claude.com/docs/en/legal-and-compliance),
[OpenAI](https://openai.com/policies/row-terms-of-use/). Notes and sources:
[docs/research/agents.md](docs/research/agents.md).

## Roadmap

- **Next:**
  - recognise Codex's update chooser in baton-detect, so a prompt never lands in it;
  - read Codex's command approvals for the policy, once one is recorded;
  - Claude's status-line limits instead of an estimate.
- **After v1:** Gemini CLI; a web panel and REST API, each with its own ADR.

Details: [docs/ROADMAP.md](docs/ROADMAP.md).

## Engineering notes

- **Event sourcing.** The SQLite log is append-only, enforced by the database. The board,
  the budget and a re-attach after a restart are all folded from it:
  - [ADR 0002](docs/adr/0002-event-log-as-source-of-truth.md);
  - `core/projection.py`;
  - the restart tests in `tests/integration/test_slice0.py`.
- **Pure decisions, thin runner.** Turn steps, recovery plans, agent choice and the policy
  are functions over data, tested as tables (`scheduler/turn.py`, `recovery/policy.py`,
  `budget/choice.py`, `policy/rules.py`). Import contracts keep `core/` free of I/O
  ([pyproject.toml](pyproject.toml), `lint-imports` in CI).
- **Strangler migration to Rust**
  ([ADR 0011](docs/adr/0011-baton-detect-in-rust.md)). The screen classifier was
  rewritten in Rust behind the same JSON contract (`schemas/`).
  - A parity test held it equal to the Python one on 1,949 screens.
  - Then batond ran both in `shadow` mode on real tasks with Claude Code and Codex,
    logging any disagreement. There was none.
  - Then the Python rules were removed.
  - Look-around rules run with a backtracking limit, and a test feeds them hostile
    screens.
- **Golden and mutation tests.**
  - [`test_classify_golden.py`](tests/integration/test_classify_golden.py) checks the
    binary's answers on 1,949 screens against
    [`tests/golden/classify.jsonl`](tests/golden/classify.jsonl). The screens are:
    - every recorded capture under every host state;
    - reset times around DST changes in seven zones;
    - edge cases.

    The answers were pinned while the Python and Rust classifiers agreed on all of them.
  - The same file deletes or loosens each rule in turn (19 mutants, loaded with
    `classify --rules`). Each one must change some answer, so no rule goes untested.
- **Evidence before rules.** Every detection rule cites a recorded screen in
  [fixtures/](fixtures/), captured with a masking tool and audited for paths, emails and
  keys in CI.
- **Reproducible benchmark.** The real loop runs on simulated time, so the same seed gives
  the same report ([bench/](bench/README.md)).
- **Decisions on record:** [ADRs 0001–0014](docs/adr/README.md). Examples:
  - automation boundaries ([0005](docs/adr/0005-automation-boundaries.md));
  - attempt identity vs. location ([0006](docs/adr/0006-attempt-identity-and-location.md));
  - remote actions ([0009](docs/adr/0009-remote-operator-actions.md));
  - Rust for reasons other than speed ([0011](docs/adr/0011-baton-detect-in-rust.md));
  - budget as a preference, not a gate ([0012](docs/adr/0012-remaining-budget.md));
  - the permission policy ([0013](docs/adr/0013-permission-policy.md)).

## Install

You need:
- Linux;
- [uv](https://docs.astral.sh/uv/);
- [herdr](https://github.com/herdrdev/herdr) 0.9.1 or later;
- the agents you want to use (Claude Code, Codex CLI, OpenCode), each signed in once by
  you.

`baton-detect`, the Rust helper, comes either from a release or from source:

```bash
# From a release (no Rust toolchain needed); x86_64 or aarch64:
curl -fsSL -o ~/.local/bin/baton-detect \
  https://github.com/selorito/baton-herdr/releases/latest/download/baton-detect-x86_64-unknown-linux-gnu
chmod +x ~/.local/bin/baton-detect

# Or from source:
cargo install --path crates/baton-detect --locked
```

baton itself installs with `uv tool install .` from a checkout, or from the wheel attached
to a release (`uv tool install ./baton_herdr-*.whl`). Then:

```bash
herdr integration install claude      # and codex / opencode: baton needs the session ids
herdr integration install codex       # these integrations report
mkdir -p ~/.config/baton && cp baton.example.toml ~/.config/baton/baton.toml
baton doctor
```

Edit `~/.config/baton/baton.toml`. At least set `[scheduler] agents` (in order of
preference) and `timezone`, because agents print limit reset times in local time. baton
reads `./baton.toml` if there is one, otherwise `~/.config/baton/baton.toml`;
`BATON_CONFIG` overrides both. Secrets go in `~/.config/baton/.env` (see `.env.example`).

`baton doctor` checks:
- the config and the database;
- herdr and its integrations;
- the agents on `PATH`;
- `baton-detect`, the policy file, the time zone and Telegram.

It says how to fix what is missing.

## Operate

- **Service:** `baton service install` writes two systemd user units:
  - `baton-herdr.service` runs a herdr server in the `baton` session;
  - `baton.service` runs `baton daemon` against it.

  Enable them with `systemctl --user enable --now baton.service` and
  `loginctl enable-linger "$USER"`.
- **Foreground:** start `herdr --session baton server &` from a plain terminal, not from
  inside an agent, then `baton daemon`.
- **Watch:**
  - `herdr --session baton` shows the agents;
  - `journalctl --user -u baton -f` shows batond's log;
  - `baton status` shows the board.
- **Data:** the event log is at `~/.local/share/baton/baton.db` (`[database] path`).
- **Screen classifier:** `baton-detect classify` (`[detector] binary`). If it stops
  answering, batond logs "detector unavailable" and retries every minute.
- **Upgrade:** `git pull && uv tool install --force . && cargo install --path
  crates/baton-detect --locked && systemctl --user restart baton`.
- **Stop:** `systemctl --user stop baton baton-herdr`.

## Development

You need [uv](https://docs.astral.sh/uv/), a stable Rust toolchain and
[just](https://just.systems/).

```bash
uv sync          # .venv with Python 3.12 and all dependencies
just check       # what CI runs: lint, types, import contracts, Python and Rust tests, fixture audit
just bench       # the benchmark → bench/results/<date>.md
just demo        # record docs/assets/demo.gif: a throwaway herdr, fake agents, real batond
just smoke       # optional: live test against an installed herdr, in a throwaway session
```

`just smoke` never touches a herdr session you have open, starts no agent, and is not part
of CI.

- Contributing: [CONTRIBUTING.md](CONTRIBUTING.md).
- Guidelines for coding agents: [CLAUDE.md](CLAUDE.md).
- Changes: [CHANGELOG.md](CHANGELOG.md).

## License

[Apache-2.0](LICENSE).
