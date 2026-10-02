# Related projects (read-only study)

Read on **2026-09-30** from the projects' public repositories. No code was copied; this is a
record of how they work and which ideas baton may reuse. Licences are as reported by the
GitHub API and the files in each repository on that date.

## herdr-plugin-agent-quota

Repository: <https://github.com/kwanwooi25/herdr-plugin-agent-quota> (the project found for
"herdr-agent-quota"; last push 2026-08-30).

**Licence: none.** The repository has no `LICENSE` file and the GitHub API reports
`license: null`. Without a licence the code is all rights reserved: baton must not copy or
adapt its source. Observing its behaviour is fine.

**Where it reads quota** (`lib/limits.js`, header comment and code):

| Path | Source | Compatible with ADR 0005? |
|------|--------|---------------------------|
| Claude, live (preferred) | `GET https://api.anthropic.com/api/oauth/usage` with the Claude Code OAuth access token, read from the macOS Keychain item "Claude Code-credentials" or from `~/.claude/.credentials.json` | **No.** Reads the agent's credentials (rule 2) and calls a private subscription endpoint itself (rule 1). |
| Codex, live (preferred) | `GET https://chatgpt.com/backend-api/wham/usage` with the access token from `~/.codex/auth.json` | **No**, for the same two reasons. |
| Claude, fallback | `rate_limits` in status line payloads cached by a HUD under `~/.claude/hud/cache/stdin.*.json` | Yes: status line data, no credentials. This is the source ADR 0005 allows and `agents.md` documents. |
| Codex, fallback | `rate_limits` on `token_count` events in recent rollout files under `~/.codex/sessions` | Yes: the same source baton plans to use. |
| Grok | through the agent CLI's own stored session | Out of scope for baton. |

**Verdict.** Its preferred path is exactly what ADR 0005 forbids; its fallback path is what
baton will do. Two useful confirmations: both vendors expose a usage endpoint behind the
subscription login (so more precise data exists, but only by handling credentials), and the
plugin's comment that some cached status line payloads "hold stale rate_limits despite fresh
mtimes" is a warning for baton's Claude quota reader: trust `resets_at`, not the file's age.
That staleness claim is theirs and is UNVERIFIED here.

## herdr-supervisor

Two unrelated projects share the name.

| Repository | Licence | What it is |
|------------|---------|------------|
| <https://github.com/Ejlonn/herdr-supervisor> | **Apache-2.0** (`LICENSE` file; API `Apache-2.0`) | Python. Human-in-the-loop orchestration for agents in herdr with a Telegram bridge, quota waiting, backup. The one with the Telegram security design. |
| <https://github.com/hao1939/herdr-supervisor> | MIT | TypeScript. One Pi agent supervising herdr workers against goals. No Telegram. |

Apache-2.0 is the same licence as baton, so reuse with attribution would be permitted. The
list below is nevertheless ideas only, to be implemented in baton's own design.

### Ideas from Ejlonn/herdr-supervisor's Telegram design

From `docs/SECURITY.md`, `docs/TELEGRAM.md` and `src/herdr_telegram.py`:

1. **Owner and chat lock.** Every state-changing action requires the configured numeric owner
   user id **and** the exact private chat id; updates from chats that are not private are
   rejected outright. Actionable mode refuses to start without an exact `chat_id`.
2. **No inbound surface.** Long polling only: no listener, webhook or public URL; only
   outbound TLS to `api.telegram.org`. A webhook conflict fails closed.
3. **Callback binding.** Button callback data is an opaque id, not the action. The id maps to
   a stored record that is expiring, one-time, and bound to the run, the gate or decision, the
   expected state and hashes of the artifact and payload being approved. At click time the
   record is checked against **live** state, never against what the card displayed; a stale or
   mismatched click does nothing.
4. **Durable before consumed.** The command is written durably first, and the token is
   consumed second, so a crash in between re-enqueues the same request instead of losing or
   doubling it. Ambiguous Telegram deliveries of actionable cards are recorded, not blindly
   retried.
5. **Reply intents.** "Answer this question" opens a one-message, short-lived (30 minutes in
   their defaults) intent bound to the exact run, decision, owner and chat. Free text is only
   accepted while such an intent is open, and only once.
6. **Token handling.** The bot token lives in a mode-0600 file, is entered without echo, is
   never on a command line or in the environment, and is redacted from errors and logs.
7. **Shadow mode.** A fresh setup starts read-only ("shadow") and must be switched to
   actionable deliberately; a `doctor` command separates unconfigured, DNS / HTTPS,
   authentication and conflict failures.
8. **Text is data.** Task text and uploaded files are never turned into shell command
   fragments; uploads are type- and size-checked, stored under opaque names, previewed, and
   started only after explicit confirmation.
9. **Pane ownership repair.** When a saved session is duplicated, moved or has lost its pane,
   the bot says so, adopts no other conversation and resends nothing until the operator
   confirms a repair. This is the same stance as baton's ADR 0006.
10. **Quota waiting.** It waits until the latest blocking window's `resets_at` plus a buffer
    before continuing (`src/herdr_quota.py`), and shows waiting / available cards.

### What baton should take

- Items 1, 2, 3, 4 and 5 as requirements for the Telegram step of the roadmap. In baton's
  terms a callback record is bound to `(task_id, attempt_id, the observation it answers)` and
  is resolved through `core.targeting` before anything is sent.
- Item 6 conflicts with baton's current config, which reads `BATON_TELEGRAM__BOT_TOKEN` from
  the environment or `.env`. Decide in the Telegram ADR whether to add a token-file option
  and make it the documented default.
- Item 7 as a `dry-run` mode for the bot.
- Item 10 matches the budget design; add a configurable buffer after `resets_at`.

Not taken: its task workflow, gates and backup features, which are a different product shape.
