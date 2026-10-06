# fixtures

Real screens, herdr classifications, status events and usage-log samples recorded from coding
agents. Detection and adapter tests replay these files; nothing here is hand-written except
the READMEs.

## Layout

```
fixtures/
  herdr/                                   herdr API schema and active detection manifests
  <agent>/                                 claude, codex, gemini, opencode
    <scenario>/<UTC timestamp>/
      screen.txt                           `pane read --source recent`, ANSI stripped
      screen.ansi                          the same rows with ANSI styling
      screen.detection.txt                 `pane read --source detection`: what herdr classifies
      explain.json                         `agent explain --json`: herdr's state, rule and manifest version
      pane.json                            `pane get`
      meta.json                            agent CLI version, herdr version, rows, time, note
    _events/<UTC timestamp>.ndjson         pane.agent_status_changed events from `watch`
    usage-sample.jsonl                     redacted structure of the agent's usage log
```

Scenarios are a fixed list: `idle`, `working`, `blocked_permission`, `blocked_question`,
`done`, `rate_limited`, `context_full`, `crashed`, `resume_prompt`.

## Capturing a screen

1. Start herdr and open a pane **inside `~/dev/baton-sandbox`** (the toy repository; for a
   sandbox made under the project's earlier name, pass its path with `--sandbox`). The tool
   refuses to save a pane whose working directory is anywhere else unless you pass
   `--allow-outside-sandbox`; do not use that for commits.
2. Start the agent in that pane and bring it into the scenario's state.
3. From another terminal or pane, in the coban repository:

   ```bash
   uv run tools/capture/capture.py --pane w1:p2 --agent claude --scenario idle
   uv run tools/capture/capture.py --pane w1:p2 --agent claude --scenario idle --note "why this screen is interesting"
   ```

   Find the pane id with `herdr pane list` or `herdr pane current`. `--lines N` (default 200)
   sets how many recent rows to keep.
4. To record status transitions, run this before triggering them and stop it with Ctrl+C:

   ```bash
   uv run tools/capture/capture.py watch --pane w1:p2
   ```

   It subscribes to `pane.agent_status_changed` on herdr's socket (the CLI has no
   streaming command) and writes each event with the time it was received. If herdr reports
   `events_lost`, the recording stops with an error instead of leaving a silent gap.
5. Look at the files, then run `just fixtures-audit` and commit.

## Masking

Every file is masked **before** it is written:

| What | Replaced with |
|------|---------------|
| Anthropic, OpenAI, Google, GitHub, Slack and AWS key formats, JWTs, bearer tokens, private key blocks | `<anthropic_key>`, `<openai_key>`, … |
| `key=value` / `"key": "value"` where the key names a token, secret, password or API key (plain numbers are kept) | `<redacted>` value |
| Email addresses | `<email>` |
| The home directory | `~` |
| This machine's user name and host name | `<user>`, `<host>` |
| Other users' home directories | home prefix plus `<user>` |

`just fixtures-audit` (part of `just check` and CI) rescans the whole tree and fails on any
key pattern, email address, absolute home path, the current user or host name, an encoded
project directory other than the sandbox's, or a home-relative path outside the sandbox and herdr's
own config and state directories. If it fails, fix or delete the capture; do not weaken the
audit to make a capture pass.

Usage samples (`usage-sample.jsonl`) go further: a whitelist keeps numbers, booleans and
the values of `type`, `subtype`, `role`, `model`, `stop_reason`, `service_tier`, `timestamp`
and `status`. Every other string becomes `<redacted>`, UUIDs become stable fake UUIDs, and
object keys that are not plain identifiers (for example file paths used as keys) are replaced.
Prompts, code, working directories and branch names never survive. Generate them only from
sandbox sessions:

```bash
uv run tools/capture/capture.py usage-sample --agent claude --input <transcript.jsonl>
uv run tools/capture/capture.py usage-sample --agent opencode --input <opencode.db>
```

## Rare screens

Rate-limit and full-context screens cannot be produced on demand without burning quota.
They are added here when they happen in real use: capture the pane as soon as the screen
appears, in the sandbox if possible, and commit it with a `--note` describing what led to it.
