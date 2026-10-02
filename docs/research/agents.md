# Agent research (H0)

Facts about the four coding agents baton will drive, gathered on **2026-09-24** to design
the M2 adapters, the detector and usage accounting. Every row names its source:

- **verified locally**: observed on this machine (versions below), in real logs or by running
  a command that does not spend quota.
- **official docs** / **source code**: read from the vendor's documentation or from the
  agent's source at the tag of the installed version; linked.
- **GitHub issue**: reported by users; shows real-world text, not a guarantee.
- **UNVERIFIED**: plausible but not confirmed by any of the above. Do not build on it without
  checking.

Screen texts marked "pending capture" will be confirmed by the sandbox captures in
`fixtures/<agent>/`.

## Environment

| Component | Version | Install method | Source |
|-----------|---------|----------------|--------|
| herdr | 0.9.1 (stable, protocol 22) | GitHub release binary, SHA256 checked, `~/.local/bin/herdr` | verified locally |
| Claude Code | 2.1.281 installed on 2026-09-24; auto-updated to 2.1.285 before the captures | native installer `curl -fsSL https://claude.ai/install.sh \| bash` | verified locally; [official docs](https://code.claude.com/docs/en/setup) |
| Codex CLI | 0.156.1 | `npm install -g @openai/codex` | verified locally; [README](https://github.com/openai/codex) |
| Gemini CLI | 0.61.0 | `npm install -g @google/gemini-cli` (needs Node ≥ 20, `engines` in package.json) | verified locally; [README](https://github.com/google-gemini/gemini-cli) |
| OpenCode | 1.18.32 | `curl -fsSL https://opencode.ai/install \| bash` → `~/.opencode/bin` | verified locally; [official docs](https://opencode.ai/docs/) |
| Node.js | v24.21.0 (nvm `lts/*`); nvm default left at v18.20.8 | `nvm install --lts` | verified locally |

Codex and Gemini are npm packages. They run through wrappers in `~/.local/bin/` that put
Node v24 first on `PATH`, so the machine's default Node (v18) stays unchanged.

## Cross-agent summary

| Topic | Claude Code | Codex CLI | Gemini CLI | OpenCode |
|-------|-------------|-----------|------------|----------|
| Resume by id | `claude --resume <id>` | `codex resume <id>` | `gemini --resume <id>` | `opencode --session <id>` |
| Session id reported to herdr (`agent_session`) | yes, with integration | yes, with integration | **no integration** | yes, with integration |
| Usage log | JSONL transcript | JSONL rollout | JSONL chat file | SQLite |
| Structured quota source | status line `rate_limits` | rollout `token_count.rate_limits` | none found | none found (provider-dependent) |
| Reset time in limit text | `resets 3:45pm` (no date, no zone) | `try again at Feb 23rd, 2026 9:01 PM` or `try again in 3 hours 2 minutes` | `Access resets at <h:mm AM/PM ZONE>` | `It will reset in <duration>` (OpenCode Go/Zen) or provider text |
| herdr rule for limits / full context | none | none | none | none |

## Claude Code

| Topic | Finding | Source |
|-------|---------|--------|
| Tested version | Captures: 2.1.285 (the native install updated itself from 2.1.281 without asking). Local transcripts span 2.1.207–2.1.280. | verified locally |
| Launch | `claude` in the project directory. | [official docs](https://code.claude.com/docs/en/setup) |
| Resume | `claude --resume <session-id>` (works from any directory since 2.1.223), `claude --continue` (latest in cwd), `claude --resume` (picker), `--fork-session` to branch. Unknown id → `No conversation found with session ID: <id>`. | [official docs](https://code.claude.com/docs/en/sessions) |
| Resume after long idle | Pro/Max: resuming a session idle > ~1 h and > 100k tokens opens a "Resume from summary / as-is / don't ask again" dialog before the first message. This is a resume-time blocker the adapter must answer. | [official docs](https://code.claude.com/docs/en/sessions#resume-from-a-summary) |
| herdr's resume command | `claude --resume <id>`, using the id reported by herdr's Claude integration (hook v6). | [herdr source v0.9.1 `src/agent_resume.rs`](https://github.com/herdrdev/herdr/blob/v0.9.1/src/agent_resume.rs); [herdr docs 0.9.1](https://github.com/herdrdev/herdr/blob/master/docs/versions/0.9.1/website/src/content/docs/integrations.mdx) |
| Where the session id comes from | (1) transcript file name `<session-id>.jsonl`; (2) `sessionId` on every transcript record; (3) `session_id` in hook and status line input; (4) `agent_session` in `herdr pane get` when the herdr integration is installed. | (1)(2) verified locally; (3) [official docs](https://code.claude.com/docs/en/statusline); (4) herdr docs |
| Transcript location | `~/.claude/projects/<cwd with non-alphanumerics → '-'>/<session-id>.jsonl`; moved by `CLAUDE_CONFIG_DIR`; 30-day retention (`cleanupPeriodDays`). | verified locally; [official docs](https://code.claude.com/docs/en/sessions#where-transcripts-are-stored) |
| Transcript stability | "The entry format is internal to Claude Code and changes between versions." Parsers must be tolerant and version-aware. | [official docs](https://code.claude.com/docs/en/sessions#where-transcripts-are-stored) |
| Transcript format | JSONL. Record `type`s seen: `user`, `assistant`, `attachment`, `system`, `file-history-snapshot`, `file-history-delta`, `queue-operation`, `last-prompt`, `ai-title`, `mode`, `bridge-session`, `atis-latch`, `cost-state`. 1 of the lines seen contained a raw control character (needs `strict=False`), and 1 line did not parse at all. | verified locally |
| Token fields | `assistant.message.usage`: `input_tokens`, `output_tokens`, `cache_creation_input_tokens`, `cache_read_input_tokens`, plus `cache_creation{…}`, `server_tool_use{…}`, `service_tier`, `speed`, `iterations`. `message.model` names the model. | verified locally |
| Aggregation | Sum `assistant` records only, **deduplicated by `message.id`**: 1570 of 3222 usage rows repeated an id, and 25 of those carried different numbers (streaming updates). Keep the last row per id. Skip `model == "<synthetic>"`: these are locally generated API-error messages (`error`: `authentication_failed`, `server_error` seen). A `cost-state` record (2.1.280) carries `totalCostUSD` and `modelUsage`. | verified locally |
| Quota / reset data | Not in transcripts. The status line command receives `rate_limits.five_hour` / `rate_limits.seven_day` with `used_percentage` (0–100) and `resets_at` (Unix seconds), for Pro/Max only, after the first API response. `/usage` shows the windows interactively. | transcripts: verified locally; [official docs](https://code.claude.com/docs/en/statusline) |
| Limit message | `You've hit your session limit · resets 3:45pm`, `You've hit your weekly limit · resets Mon 12:00am`, and the same for `Opus limit` / `Sonnet limit`. Warning before: `You've used 85% of your session limit · resets 3:45pm`. No date or time zone in the examples. Screen rendering: pending capture. | [official docs](https://code.claude.com/docs/en/errors#youve-hit-your-session-limit) |
| Other limit texts | `Server is temporarily limiting requests`, `Request rejected (429)`, `API Error: Repeated 529 Overloaded errors`, `Credit balance is too low`. | [official docs](https://code.claude.com/docs/en/errors) |
| Context full | Auto-compact is on by default. When it cannot help: `Context limit reached · /compact or /clear to continue` (plus `· auto-compact is off · /config to turn it on` since 2.1.235), `Prompt is too long`. Status line gives `context_window.used_percentage`. | [official docs](https://code.claude.com/docs/en/errors#prompt-is-too-long) |
| Compaction in the log | `system` record, `subtype: "compact_boundary"`, `compactMetadata{trigger, preTokens, postTokens, durationMs, cumulativeDroppedTokens, …}`; the summary message has `isCompactSummary: true`. Only `trigger: "manual"` was seen; the automatic value is UNVERIFIED. | verified locally |
| herdr detection (manifest 2026.09.11.1) | working: `osc_title_working` (spinner in title), `live_turn_working` (`esc to interrupt` / activity line), `background_agents_working`, `background_mcp_task_working`, `btw_overlay_working`. blocked: `live_blocked_form`, `dynamic_workflow_prompt`, `mcp_elicitation_prompt`, `bash_permission_prompt`, `generic_permission_prompt`, `legacy_no_prompt_blocker`. idle: `live_prompt_box`, `osc_title_idle`, `osc_progress_idle`. skip: `transcript_viewer`, `model_picker_menu`. | `fixtures/herdr/agent-detection/claude-2026.09.11.1.toml` |
| Gaps for baton | No rule for session/weekly limit, `Context limit reached`, crash / process exit, the resume picker, or the "resume from summary" dialog. Status is the herdr enum `idle / working / blocked / done / unknown` only. A custom status line hides the `esc to interrupt` hint that `live_turn_working` matches (the spinner-title rule still applies). | manifest; [official docs](https://code.claude.com/docs/en/statusline) |
| Terms (automation) | Pro/Max use falls under the Consumer Terms. OAuth sign-in is "designed to support ordinary use of Claude Code and other native Anthropic applications"; third parties may not "collect, store, or intermediate Claude.ai credentials" or route requests through a user's plan on their behalf. An end user signing in to the unmodified binary is allowed. "Advertised usage limits for Pro and Max plans assume ordinary, individual usage." Not legal advice. | [official docs: Legal and compliance](https://code.claude.com/docs/en/legal-and-compliance) |

## Codex CLI

| Topic | Finding | Source |
|-------|---------|--------|
| Tested version | 0.156.1. Local rollouts span 0.78.0–0.142.5. | verified locally |
| Launch | `codex` in the project directory. | [README](https://github.com/openai/codex) |
| Resume | `codex resume [SESSION_ID]` (UUID or name), `--last` (latest in cwd), `--all` (any directory); `codex exec resume` for non-interactive runs. | [official docs](https://learn.chatgpt.com/docs/developer-commands?surface=cli) |
| herdr's resume command | `codex resume <id>` from the herdr Codex integration (hook v5). | [herdr source v0.9.1 `src/agent_resume.rs`](https://github.com/herdrdev/herdr/blob/v0.9.1/src/agent_resume.rs); herdr docs |
| Where the session id comes from | Rollout file name `rollout-<timestamp>-<uuid>.jsonl` and `session_meta.payload.id` (first record); `agent_session` in herdr with the integration. On 0.156.1, `codex resume <id>` followed by a new turn **appended to the same rollout file** (one `session_meta`, 92 → 103 records); older reports of a second file ([discussion #3827](https://github.com/openai/codex/discussions/3827)) did not reproduce. | verified locally; herdr docs |
| Log location | `~/.codex/sessions/YYYY/MM/DD/rollout-*.jsonl` (`CODEX_HOME` moves `~/.codex`). Also present: `session_index.jsonl`, `state_5.sqlite`, `logs_2.sqlite` (contents not inspected). | verified locally; `CODEX_HOME`: herdr docs |
| Log format | JSONL `{timestamp, type, payload}`. Counts across 21 local files: `event_msg/token_count` 4925, `response_item/*` (message, reasoning, function_call, …), `event_msg/{task_started, task_complete, turn_aborted, context_compacted, user_message, agent_message, …}`, `turn_context`, `session_meta`, `compacted`. | verified locally |
| Token fields | `event_msg/token_count.payload.info`: `total_token_usage` and `last_token_usage`, each `{input_tokens, cached_input_tokens, output_tokens, reasoning_output_tokens, total_tokens}`, plus `model_context_window`. `info` is `null` on the first event of a session. | verified locally |
| Aggregation | `total_token_usage` is cumulative per session: use the last `token_count` in a rollout, or sum `last_token_usage`. `cached_input_tokens` looked like a subset of `input_tokens` (17.1 M of 17.9 M in one session); this is inferred from the numbers, not documented. After a resume the cumulative total continued (142108 → 157002 `total_tokens`). | verified locally |
| Quota / reset data | Every `token_count` carries `rate_limits`: `primary` (`window_minutes: 300`) and `secondary` (`window_minutes: 10080`), each `{used_percent, window_minutes, resets_at}` with `resets_at` in Unix seconds, plus `limit_id`, `plan_type`, `credits`, `rate_limit_reached_type`. Fields differ between versions (older files have no `limit_id`). **This is the best structured quota source of the four agents.** | verified locally |
| Limit message | `You've hit your usage limit. Upgrade to Pro (…) or try again in 3 hours 2 minutes.` (0.27.0); `You've hit your usage limit. Upgrade to Pro (…), visit https://chatgpt.com/codex/settings/usage to purchase more credits or try again at Feb 23rd, 2026 9:01 PM.`; business seats: `… send a request to your admin or try again at Apr 12th, 2026 3:31 PM.` The absolute time has no zone (local time assumed: UNVERIFIED). Text for 0.156.1: pending capture. | GitHub issues [#3031](https://github.com/openai/codex/issues/3031), [#12299](https://github.com/openai/codex/issues/12299), [#16917](https://github.com/openai/codex/issues/16917) |
| Context full | Log shows `compacted` records and `event_msg/context_compacted` (22 each locally); `model_context_window` gives the size. The on-screen text and the auto-compaction threshold are UNVERIFIED. | verified locally |
| herdr detection (manifest 2026.09.23.1) | blocked: `osc_title_blocked` (`Action Required` in title), `trust_directory`, `startup_update`, `live_strong_blocker`, `weak_blocker`. working: `osc_title_working` (braille spinner), `screen_working_fallback` (timer line). skip: `transcript_viewer`. No idle rule: idle is herdr's fallback (UNVERIFIED how). | `fixtures/herdr/agent-detection/codex-2026.09.23.1.toml` |
| Gaps for baton | No rule for the usage-limit message, compaction, crash, or the `codex resume` picker. `startup_update` blocks on "Update available!", so an unattended start can stall until answered. | manifest |
| Terms (automation) | ChatGPT-plan sign-in falls under OpenAI's Terms of Use. A clause prohibiting "automatically or programmatically extract[ing] data or Output" and circumventing rate limits is reported: UNVERIFIED (openai.com and help.openai.com returned 403 to the fetch; seen only in a search summary). Codex CLI itself is Apache-2.0. | [Terms of Use](https://openai.com/policies/row-terms-of-use/) (not fetched); [help article](https://help.openai.com/en/articles/11369540-using-codex-with-your-chatgpt-plan) (not fetched) |

## Gemini CLI

| Topic | Finding | Source |
|-------|---------|--------|
| Tested version | 0.61.0. No local sessions before this study. | verified locally |
| Launch | `gemini` in the project directory. | [README](https://github.com/google-gemini/gemini-cli) |
| Resume | `gemini --resume` (latest), `gemini --resume <index>` or `<uuid>`, `/resume` in-session browser, `gemini --list-sessions`, `gemini --delete-session <n>`. | [official docs](https://github.com/google-gemini/gemini-cli/blob/main/docs/cli/session-management.md) (main branch) |
| herdr's resume command | None. herdr 0.9.1 has **no Gemini CLI integration**, so it neither reports a session id nor restores Gemini panes. | [herdr docs 0.9.1: integrations](https://github.com/herdrdev/herdr/blob/master/docs/versions/0.9.1/website/src/content/docs/integrations.mdx) |
| Where the session id comes from | Session UUID in the chat file (`sessionId`), and `--list-sessions`. baton has to obtain it itself. | [source v0.61.0](https://github.com/google-gemini/gemini-cli/blob/v0.61.0/packages/core/src/services/chatRecordingService.ts) |
| Log location | `~/.gemini/tmp/<project_hash>/chats/`; JSONL since the `.json` → `.jsonl` migration (0.61.0 writes `.jsonl`). Exact file names: pending local run. | [official docs](https://github.com/google-gemini/gemini-cli/blob/main/docs/cli/session-management.md); source v0.61.0 |
| Token fields | Per model message `tokens`: `input`, `output`, `cached`, `thoughts`, `tool`, `total` (mapped from the API's `promptTokenCount`, `candidatesTokenCount`, `cachedContentTokenCount`, `thoughtsTokenCount`, `toolUsePromptTokenCount`, `totalTokenCount`). | source v0.61.0 `chatRecordingService.ts` |
| Aggregation | Sum per message. Whether `cached` is included in `input` is UNVERIFIED. Quotas are counted in **requests** (per minute, per day), not tokens, so token sums do not predict the limit. | [official docs: quotas](https://github.com/google-gemini/gemini-cli/blob/main/docs/resources/quota-and-pricing.md) |
| Limit message | `Usage limit reached for <model>.` / `Access resets at <time>.` / `/stats model for usage details` / `/model to switch models.` The time comes from `Intl.DateTimeFormat('en-US', {hour: 'numeric', minute: '2-digit', timeZoneName: 'short'})`: a time and a short zone, **no date**. Capacity: `We are currently experiencing high demand for <model>.` After fallback: `Switched to fallback model <model>`. On-screen rendering: pending capture. | [source v0.61.0 `useQuotaAndFallback.ts`](https://github.com/google-gemini/gemini-cli/blob/v0.61.0/packages/cli/src/ui/hooks/useQuotaAndFallback.ts) |
| Context full | `/compress` replaces history with a summary; automatic compression at `model.compressionThreshold` (default `0.5` of the context). The on-screen signal is UNVERIFIED. | [official docs: commands](https://github.com/google-gemini/gemini-cli/blob/main/docs/reference/commands.md), [configuration](https://github.com/google-gemini/gemini-cli/blob/main/docs/reference/configuration.md) |
| herdr detection (manifest 2026.06.10.1) | Only two rules: blocked `apply_or_allow_change`, working `esc_cancel_working` (`esc to cancel` anywhere in the recent screen). No idle rule. | `fixtures/herdr/agent-detection/gemini-2026.06.10.1.toml` |
| Gaps for baton | Idle, questions, the quota dialog, model fallback, compression and resume are undetected. With no integration, all session and state information must come from baton. This is the weakest-covered agent. | manifest; herdr docs |
| Terms (automation) | Gemini CLI is Apache-2.0; Google service terms depend on the sign-in method. "Directly accessing the services powering Gemini CLI … using third-party software … (for example, using OpenClaw with Gemini CLI OAuth) is a violation" and may lead to suspension. baton must drive the `gemini` binary and never reuse its OAuth. | [official docs v0.61.0 `tos-privacy.md`](https://github.com/google-gemini/gemini-cli/blob/v0.61.0/docs/resources/tos-privacy.md) |

## OpenCode

| Topic | Finding | Source |
|-------|---------|--------|
| Tested version | 1.18.32. No local sessions before this study. | verified locally |
| Launch | `opencode` (TUI) in the project directory. | [official docs](https://opencode.ai/docs/) |
| Resume | `opencode --continue` / `-c` (last session), `--session` / `-s <id>`, `--fork`; `opencode session list [--format json]`. | [official docs: CLI](https://opencode.ai/docs/cli/) |
| herdr's resume command | `opencode --session <id>`, using the id from herdr's OpenCode plugin (v5, needs OpenCode ≥ 1.18.29). The plugin also reports lifecycle state (working / idle / blocked), unlike the Claude and Codex hooks. | [herdr source v0.9.1 `src/agent_resume.rs`](https://github.com/herdrdev/herdr/blob/v0.9.1/src/agent_resume.rs); herdr docs |
| Where the session id comes from | `session.id` in the database; `opencode session list`; `agent_session` in herdr with the plugin. | schema verified locally; official docs |
| Storage | SQLite, `~/.local/share/opencode/opencode.db` (`opencode db path`), WAL mode. Tables include `session`, `message`, `part`, `event`, `project`. `message.data` and `part.data` are JSON text. | verified locally |
| Token fields | `session` row totals: `tokens_input`, `tokens_output`, `tokens_reasoning`, `tokens_cache_read`, `tokens_cache_write`, `cost`, `model`. Per assistant message in `message.data`: `tokens{total, input, output, reasoning, cache{read, write}}`, `cost`, `modelID`, `providerID`, `finish`. `opencode stats [--days N] [--models]` prints totals. | schema verified locally; [official docs: CLI](https://opencode.ai/docs/cli/) |
| Aggregation | Read the `session` totals from a read-only SQLite connection (the file is live and in WAL mode). | verified locally (schema) |
| Limit message | Provider-dependent. OpenCode's own retry layer shows `Free limit reached`, `<name> usage limit reached. It will reset in <duration>. …` (OpenCode Go), `Provider is overloaded`, `Too Many Requests`, or the provider's message, and retries on `retry-after` headers. On-screen rendering: pending capture. | [source v1.18.32 `session/retry.ts`](https://github.com/anomalyco/opencode/blob/v1.18.32/packages/opencode/src/session/retry.ts) |
| Context full | UNVERIFIED (not researched in the docs; pending capture). | — |
| herdr detection (manifest 2026.06.10.1) | blocked `permission_required`; working `interrupt_hint_working`, `progress_bar_working`. No idle rule: with the plugin installed, the plugin is the lifecycle authority. | `fixtures/herdr/agent-detection/opencode-2026.06.10.1.toml`; herdr docs |
| Gaps for baton | Limit / retry states, context full, crash and resume are undetected on screen. | manifest |
| Terms (automation) | OpenCode is MIT-licensed. Model access follows each provider's terms; for example, Anthropic does not permit third-party apps to "offer Claude.ai login" or route requests through a user's Pro/Max plan. | [license](https://github.com/anomalyco/opencode); [Anthropic legal page](https://code.claude.com/docs/en/legal-and-compliance) |

## Findings that shape M2

1. **herdr's status enum is too small for baton.** `idle / working / blocked / done / unknown`,
   and no active manifest has a rule for usage limits, full context, crashes or resume
   pickers. baton-detect has to add these states on top of herdr's status, not replace it.
2. **The active detection rules are a moving remote catalog**, not the rules bundled in the
   herdr binary (Codex's manifest changed the day before this study). Fixtures must record
   the manifest version (`explain.json` does), and baton should not copy herdr's rules.
3. **Structured quota data exists for two agents.** Codex writes `rate_limits` (5 h and 7 d
   windows, `resets_at` in Unix seconds) to every rollout. Claude exposes the same windows only
   to a status line command, which has a side effect on herdr's detection (hides
   `esc to interrupt`). Gemini and OpenCode leave only screen text, so the budget module needs a
   screen-text fallback with per-agent parsers.
4. **Reset times in screen text are ambiguous.** Claude: `3:45pm` / `Mon 12:00am` with no
   date or zone; Codex: an absolute date without a zone, or a relative duration; Gemini: a time
   with a zone but no date. Parsers must resolve against the capture time and the local zone,
   and prefer structured sources.
5. **Session identity.** herdr integrations report session ids for Claude, Codex and
   OpenCode; Gemini has none, so the Gemini adapter must find its session in
   `~/.gemini/tmp/<hash>/chats/`.
6. **Usage logs are internal formats.** Claude explicitly says its transcript format changes
   between versions. Parsers must tolerate unknown records, raw control characters and broken
   lines, dedupe Claude rows by `message.id`, and be tested against version-tagged samples.
7. **Resume can itself block.** Claude's "resume from summary" dialog, Codex's
   `Update available!` screen and directory-trust prompts need adapter answers or human
   escalation.
8. **Terms.** baton must only drive the unmodified official binaries that the user signed in
   to, never read or store their credentials, and treat "ordinary, individual usage" (Anthropic)
   as a constraint on how aggressively it resumes work. This deserves an ADR before M2.
9. **Pane width is not exposed by herdr 0.9.1** (`PaneInfo` has `scroll.viewport_rows` only),
   so fixtures record `cols: null`.

## herdr CLI notes

JSON output shapes of the herdr 0.9.1 commands baton uses, verified locally on 2026-09-29
against a running server:

| Command | Success (stdout, exit 0) | Failure (stderr, exit 1) |
|---------|--------------------------|--------------------------|
| `pane get <id>`, `pane list`, `server agent-manifests --json` | `{"id", "result": {...}}` | `{"id", "error": {"code", "message"}}` |
| `agent explain <id> --json` | **bare object** `{"agent", "state", "matched_rule", "fallback_reason", "evaluated_rules", …}` | `{"id", "error"}`, e.g. `agent_not_found` for a pane without an agent |
| `api schema --json` | **bare object** (the JSON Schema) | not observed |
| `pane read <id>` | plain text | `{"id", "error"}`, e.g. `pane_not_found` |

A client must accept both success shapes and read the error envelope from stderr.

On Claude Code 2.1.281's first-run theme picker, `agent explain` returned `state: "idle"`
with `matched_rule: null` and `fallback_reason: "default_known_agent_idle_fallback"`: herdr
reports idle for a known agent when no rule matches, even though the agent is waiting for a
choice. baton must not treat herdr's `idle` as "ready for a prompt" without its own check.

## Capture findings (2026-09-30)

Observed while recording `fixtures/<agent>/` in the sandbox, driving the agents through the
herdr CLI (`herdr agent prompt`, `herdr agent wait --until …`, `herdr pane send-keys`). All
rows are **verified locally**; the capture directory holding the evidence is named.

### Across agents

1. **herdr's `idle` is a fallback, not a positive signal.** Whenever a known agent shows a
   screen no rule matches, `agent explain` returns `state: idle`,
   `fallback_reason: default_known_agent_idle_fallback`. This covered every first-run dialog
   and every resume picker below. baton needs its own "ready for a prompt" check.
2. **`done` depends on what the human is looking at.** herdr reports `done` only when a turn
   ends in a tab that is not active; in the active tab of a focused window it goes straight to
   `idle` (herdr `src/app/actions.rs`, `active_tab_suppresses_notifications`). Infer completion
   from the `working → idle|done` transition in the event stream, not from `done`.
3. **A crash reads as `agent: null`, `agent_status: unknown`**, and `agent explain` fails with
   `agent_not_found`. That state is also what a plain shell pane looks like, so it is not a
   crash signal by itself: combine it with `herdr pane process-info` and the previous status.
   (`fixtures/*/crashed/`)
4. **`kill -9` can leave the terminal in a bad state.** After killing Claude Code, mouse
   reporting stayed enabled and mouse movement was typed into the shell as commands; `reset`
   did not recover the pane. The recovery engine should restart an agent in a fresh pane, or
   reset the terminal and verify it before launching.
5. **Agents update themselves.** Claude Code went 2.1.281 → 2.1.285 and Gemini CLI printed
   "Update successful! The new version will be used on your next run" with no prompt; Codex
   asks on every start. Fixtures must record the CLI version (they do, in `meta.json`), and
   detection rules need to be checked against new versions continuously.
6. **Permission and question are told apart by herdr for Claude** (`bash_permission_prompt`
   vs `live_blocked_form`), which lets baton choose between approve/deny buttons and a free-text
   reply in Telegram. For other agents baton has to classify the blocker itself.
7. **Timing a capture by hand does not work.** Use
   `herdr agent wait <pane> --until working|blocked|done` and capture when it returns.

### Claude Code 2.1.285

| Observation | Evidence |
|-------------|----------|
| The default permission mode is now auto mode: a Bash command did not raise a permission prompt. `--permission-mode default` was needed to see one. Blocked-on-permission will be rarer than the manifest suggests. | `blocked_permission/` (captured with `--permission-mode default`) |
| Permission prompt → `blocked`, rule `bash_permission_prompt`. Question tool dialog → `blocked`, rule `live_blocked_form`. Working → `osc_title_working`. Idle → `live_prompt_box`. | `blocked_permission/`, `blocked_question/`, `working/`, `idle/` |
| First-run theme picker and the `claude --resume` session picker are classified `idle` by fallback. | `resume_prompt/` |
| With the herdr integration installed, `pane get` carries `agent_session {kind: "id", source: "herdr:claude", value: <uuid>}`; on exit Claude prints `claude --resume <uuid>`. | `idle/` (after resume), `pane.json` |
| Resuming a crashed session from the picker restored the conversation and showed a recap line. | `idle/` "after resuming a crashed session" |

### Codex CLI 0.156.1

| Observation | Evidence |
|-------------|----------|
| Three start-up blockers, all classified `idle` by fallback: "Trust this folder?" (herdr's `trust_directory` rule expects older wording), "Hooks need review" (after installing the herdr hook), and "Update available · 0.156.1 → 0.159.2" with Update / Skip / Skip until next version (herdr's `startup_update` rule expects older wording). The update prompt returns on **every** start, including `codex resume`. | `blocked_permission/` (first two), `blocked_question/` (update) |
| A model configured in `config.toml` that the account cannot use ends the turn with a red `{"type":"error","status":400,…}` line; herdr shows `done`. The adapter must detect error lines, not just status. | `done/` (first capture) |
| Edit approval in a read-only sandbox → `blocked`, rule `osc_title_blocked` (title contains `Action Required`). `-a untrusted` no longer exists; approval policies are `on-request` and `never`. | `blocked_permission/` (third capture) |
| Asked to ask a question, Codex asked in plain text and ended the turn: herdr reports `done`, not `blocked`. A question is indistinguishable from completion without reading the text. | `blocked_question/` (second capture) |
| Working → `osc_title_working`. There is no idle rule; idle and done both come from the fallback. | `working/`, `idle/`, `done/` |
| `codex resume` opens a picker ("Resume a previous session"), classified `idle` by fallback. `codex resume <id>` resumes directly. | `resume_prompt/` |
| Every `token_count` in the sandbox rollout carries `rate_limits` (now also `individual_limit`, `spend_control_reached`). | `usage-sample.jsonl` |

### OpenCode 1.18.32

| Observation | Evidence |
|-------------|----------|
| Works without credentials on free models (`opencode -m opencode/big-pickle`); no first-run dialogs. | `idle/` |
| With the herdr plugin, state comes from the plugin, not the screen: `agent explain` reports `working` / `blocked` with `matched_rule: null` and no fallback, and `agent_session {source: "herdr:opencode", value: "ses_…"}` is set. | `working/`, `blocked_permission/`, `pane.json` |
| Bash ran **without** a permission prompt by default. A prompt appeared for writing outside the project: "△ Permission required · Access external directory /tmp" with Allow once / Allow always / Reject. | `blocked_permission/` |
| The question tool shows a real dialog (options plus "Type your own answer") and herdr reports `blocked`. | `blocked_question/` |
| `opencode --continue` resumed the crashed session with no picker; the `/sessions` dialog is the picker. | `idle/` (after resume), `resume_prompt/` |
| The footer shows context use, e.g. `14.2K (7%)`: an on-screen context signal. | any `screen.txt` |

### Gemini CLI 0.61.0 (first-run only)

The Gemini round was **skipped**: "Sign in with Google" did not complete on this machine
(the dialog returned with "Failed to sign in. Message: Authentication timed out after 5
minutes", suggesting `NO_BROWSER=true` as an alternative). Only first-run screens were
captured. Until an authenticated round is done, everything about Gemini beyond these rows
comes from documentation and source code, and the Gemini adapter should be built last in M2.

| Observation | Evidence |
|-------------|----------|
| First-run "Do you trust the files in this folder?" dialog, then "How would you like to authenticate for this project?" (Sign in with Google / Use Gemini API Key / Vertex AI). | `blocked_permission/`, `blocked_question/` |
| During both dialogs herdr had **not detected an agent at all**: `pane get` shows `agent: null`, `agent_status: unknown`, `agent explain` fails with `agent_not_found`, and the event subscription delivered no events. Whether herdr detects Gemini after sign-in is UNVERIFIED. | `pane.json`, `explain.json` in both captures |
| A failed sign-in returns to the authentication dialog with an error line; the process stays alive and herdr still reports no agent. | `blocked_question/` (second capture) |
| On start the CLI printed "Update successful! The new version will be used on your next run." without asking. | `screen.txt` |

Not captured: idle, working, blocked on tool approval, question, done, crash, resume, the
chat log file name and a usage sample.

## Design notes for M2 / M5 (not implemented)

**1. Never trust a rule-less `idle`.** herdr's status must be read together with
`agent explain`'s `matched_rule`:

| herdr `state` | `matched_rule` | baton should treat it as |
|---------------|----------------|--------------------------|
| `working`, `blocked` | any rule, or none when an integration reports lifecycle (OpenCode plugin) | as reported |
| `idle` | a rule (`live_prompt_box`, `osc_title_idle`, …) | idle: ready for a prompt |
| `idle` | `null` with `fallback_reason: default_known_agent_idle_fallback` | **unknown**: classify the screen in baton-detect, or escalate; never send a prompt |
| `done` | any | "a turn ended", then apply the `idle` rows to decide whether it is ready |
| `unknown`, `agent: null` | — | no agent: check `pane process-info` (crash vs. never started) |

Evidence: every first-run dialog and resume picker in `fixtures/` was reported as `idle`
by fallback, for Claude (theme picker, resume picker), Codex (folder trust, hook review,
update prompt, resume picker) and OpenCode (`/sessions`). Codex has no idle rule at all, so
for Codex every `idle` is a fallback and baton-detect must supply the positive idle signal.

**2. Plain-text questions need a contract.** Codex (and any agent without a question
dialog) asks in plain text and ends the turn, which herdr reports as `done`
(`fixtures/codex/blocked_question/`, second capture). Proposed, not yet implemented:

- Every task instruction baton sends ends with a contract such as: "If you need an answer
  from me before you can continue, end your message with a final line containing exactly
  `[[BATON:QUESTION]]`."
- baton subscribes to herdr's `pane.output_matched` event for that marker (or checks the
  detection snapshot when the turn ends) and turns the pane into "waiting for an answer"
  instead of "done".
- The marker is a convention, not a guarantee: a missing marker must fall back to
  classifying the final message. The `pane.output_matched` subscription shape is in
  `fixtures/herdr/api-schema-0.9.1.json`; its matching behaviour is UNVERIFIED until tried.
- Extension, implemented in `baton_herdr.core.contract` (2026-10-03): replace the single marker with one
  small end-of-turn block that every task instruction asks for:

  ```
  [[BATON:END status=question|done|blocked]]
  ```

  - `question`: the agent needs an answer; the text above the block is the question.
  - `done`: the agent considers the task finished.
  - `blocked`: the agent cannot continue for a reason it states above the block.

  Rules for using it:
  - **Screen detection stays the main path.** The block is an additional signal. It can
    confirm or refine what the detector saw (turn a plain "turn ended" into `blocked_question`);
    it never overrides a positive detection such as a permission dialog or a limit message.
  - A missing, malformed or contradictory block means "no extra signal", never an error.
  - `done` from the block is a claim by the agent, not proof: task completion still goes
    through baton's own checks (and, where configured, a human).
  - The block is matched on the detection snapshot at the end of a turn or through
    `pane.output_matched`; only the last block of the last turn counts, so text quoted earlier
    in the conversation cannot trigger it.
  - One fixed grammar, three statuses, no free-form fields: anything richer belongs in the
    text above the block.


## First live baton runs (2026-10-03)

`baton run` drove each real agent through one small task ("add a one-line docstring to `add`
in `calc.py`") in `baton-sandbox`, in a dedicated herdr session. Versions as installed then;
Codex ran as `codex -m gpt-5.6-luna -c model_reasoning_effort=low`, OpenCode on the free
`opencode/big-pickle` model. All three completed the task with exactly the requested change.

| Agent | Duration | What baton saw |
|-------|----------|----------------|
| Claude Code | 14 s | idle by herdr rule `live_prompt_box` → prompt → working (`osc_title_working`) → idle → done. Session id learned before the prompt. |
| Codex CLI | 16 s | update prompt → answered automatically (Skip) → prompt → working → idle (`osc_title_idle`, a rule the remote catalog has added since H0). The session id arrived **after** the first prompt, so that prompt went out on the weaker kind-only check of ADR 0006. |
| OpenCode | 69 s | herdr's idle was a fallback; baton's `opencode_idle_composer` recognised the composer → prompt → working (`progress_bar_working`) → idle reported by the plugin → done. Session id arrived with the first prompt. |

One inconsistency found and fixed: once the pane status said `idle` while `agent explain`
named the working-title rule. baton now takes the state from `explain` whenever it names a
rule, and uses the pane status only when no rule matched (integration-reported state).

### Crash and resume, live (2026-10-03)

`baton daemon` ran a task on Claude Code in a herdr session started with a clean
environment. Claude was killed with `kill -9` six seconds into its work. baton recorded the
crash, reopened the same session in a fresh pane with `claude --resume <id>`, sent the
continuation note, and the task completed 11 seconds later with the full change.

An earlier attempt of the same test failed for an environmental reason worth knowing: the
herdr server had been started from a shell inside Claude Code and inherited its
`CLAUDECODE` / `CLAUDE_CODE_*` variables. Claude Code started in those panes wrote no
transcript, and `claude --resume` answered "No conversation found with session ID" and
exited. baton now recognises that refusal (`resume_failed`) and restarts a fresh session
instead of counting it as a crash; the live tests start herdr without those variables.


### Re-attach after a batond restart, live (2026-10-03)

Two runs with Claude Code, each killing `baton daemon` with `kill -9` right after the
prompt was recorded (`attempt.prompted`) and starting it again:

- **Restarted while Claude worked** (down about 10 s): the new daemon re-attached to the
  same pane, sent nothing, saw the turn go idle and completed the task. One launch, one
  prompt.
- **Claude finished while batond was down** (down about 45 s): the log had the prompt but no
  `working` observation. Re-attach treats a turn whose prompt was sent as under way, so the
  idle agent counted as finished and the task completed. This rests on the prompt having
  reached the agent; the `[[BATON:END]]` contract is what will make "done" explicit.

### End-of-turn contract, live (2026-10-03)

Every prompt now ends with the `[[BATON:END status=…]]` request. Task for Claude Code: add
docstrings, but first ask whether they should be English or Turkish.

- **First try, prompt with line breaks: failed.** herdr's `agent.prompt` delivered it as a
  paste, and Claude Code declined to act: "Your message contained only pasted text with
  nothing of your own around it, so I haven't acted on it yet." It asked in plain text,
  without a mark, and baton completed the task. Prompts are now sent as one line
  (`one_line`); task instructions lose their line breaks.
- **Second try, one line: passed.** Claude read the file, asked "Should the docstrings be in
  English or in Turkish?" above `[[BATON:END status=question]]`. baton recorded
  `blocked_question` (`baton:end:question`) and sent that question as the only notice.
  After "English" was typed at the terminal, the next daemon cycle re-attached, saw the
  agent working, then idle above `[[BATON:END status=done]]`, and completed the task.

A missing mark still counts as "no signal": the first try shows what that costs when the
agent asks in plain text anyway.

### Operator actions, live (2026-10-03)

Claude Code 2.1.287 launched with `--permission-mode default`, actions taken with
`baton approve|deny|answer` (the same `act()` the Telegram bot calls).

- **Approve.** "Run python3 -m unittest" stopped at the Bash permission prompt
  (`herdr:rule:bash_permission_prompt`). `answer` was refused (does not fit a permission
  prompt), `approve` sent Enter on "1. Yes", a second `approve` was refused as already
  answered, and the task completed.
- **Read-only commands need no permission.** `ls -la` ran without a prompt, so `deny` was
  refused: nothing was waiting.
- **Deny.** Esc on the prompt interrupts Claude's turn: "Interrupted · What should Claude do
  instead?", with no end mark. baton first counted that idle agent as finished. Now a
  denial holds until the operator answers: the attempt becomes `blocked_question`
  (`baton:denied`), the notice asks what to do instead, and the answer is sent as a
  prompt. Live: deny, notice, answer "Do not run anything", Claude replied with
  `[[BATON:END status=done]]`, task completed.
- The permission notice quotes the prompt: "claude asks for permission: Bash command ·
  Print 6 times 7 with Python · python3 -c 'print(6*7)'".
