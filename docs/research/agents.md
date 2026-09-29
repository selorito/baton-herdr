# Agent research (H0)

Facts about the four coding agents coban will drive, gathered on **2026-09-24** to design
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
| Claude Code | 2.1.281 | native installer `curl -fsSL https://claude.ai/install.sh \| bash` | verified locally; [official docs](https://code.claude.com/docs/en/setup) |
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
| Tested version | 2.1.281. Local transcripts span 2.1.207–2.1.280. | verified locally |
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
| Gaps for coban | No rule for session/weekly limit, `Context limit reached`, crash / process exit, the resume picker, or the "resume from summary" dialog. Status is the herdr enum `idle / working / blocked / done / unknown` only. A custom status line hides the `esc to interrupt` hint that `live_turn_working` matches (the spinner-title rule still applies). | manifest; [official docs](https://code.claude.com/docs/en/statusline) |
| Terms (automation) | Pro/Max use falls under the Consumer Terms. OAuth sign-in is "designed to support ordinary use of Claude Code and other native Anthropic applications"; third parties may not "collect, store, or intermediate Claude.ai credentials" or route requests through a user's plan on their behalf. An end user signing in to the unmodified binary is allowed. "Advertised usage limits for Pro and Max plans assume ordinary, individual usage." Not legal advice. | [official docs: Legal and compliance](https://code.claude.com/docs/en/legal-and-compliance) |

## Codex CLI

| Topic | Finding | Source |
|-------|---------|--------|
| Tested version | 0.156.1. Local rollouts span 0.78.0–0.142.5. | verified locally |
| Launch | `codex` in the project directory. | [README](https://github.com/openai/codex) |
| Resume | `codex resume [SESSION_ID]` (UUID or name), `--last` (latest in cwd), `--all` (any directory); `codex exec resume` for non-interactive runs. | [official docs](https://learn.chatgpt.com/docs/developer-commands?surface=cli) |
| herdr's resume command | `codex resume <id>` from the herdr Codex integration (hook v5). | [herdr source v0.9.1 `src/agent_resume.rs`](https://github.com/herdrdev/herdr/blob/v0.9.1/src/agent_resume.rs); herdr docs |
| Where the session id comes from | Rollout file name `rollout-<timestamp>-<uuid>.jsonl` and `session_meta.payload.id` (first record); `agent_session` in herdr with the integration. A resumed session is reported to write a new rollout file that keeps the original id: UNVERIFIED (only seen in a search summary of [discussion #3827](https://github.com/openai/codex/discussions/3827)). | verified locally; herdr docs |
| Log location | `~/.codex/sessions/YYYY/MM/DD/rollout-*.jsonl` (`CODEX_HOME` moves `~/.codex`). Also present: `session_index.jsonl`, `state_5.sqlite`, `logs_2.sqlite` (contents not inspected). | verified locally; `CODEX_HOME`: herdr docs |
| Log format | JSONL `{timestamp, type, payload}`. Counts across 21 local files: `event_msg/token_count` 4925, `response_item/*` (message, reasoning, function_call, …), `event_msg/{task_started, task_complete, turn_aborted, context_compacted, user_message, agent_message, …}`, `turn_context`, `session_meta`, `compacted`. | verified locally |
| Token fields | `event_msg/token_count.payload.info`: `total_token_usage` and `last_token_usage`, each `{input_tokens, cached_input_tokens, output_tokens, reasoning_output_tokens, total_tokens}`, plus `model_context_window`. `info` is `null` on the first event of a session. | verified locally |
| Aggregation | `total_token_usage` is cumulative per session: use the last `token_count` in a rollout, or sum `last_token_usage`. `cached_input_tokens` looked like a subset of `input_tokens` (17.1 M of 17.9 M in one session); this is inferred from the numbers, not documented. Whether totals continue or restart after a resume is UNVERIFIED. | verified locally |
| Quota / reset data | Every `token_count` carries `rate_limits`: `primary` (`window_minutes: 300`) and `secondary` (`window_minutes: 10080`), each `{used_percent, window_minutes, resets_at}` with `resets_at` in Unix seconds, plus `limit_id`, `plan_type`, `credits`, `rate_limit_reached_type`. Fields differ between versions (older files have no `limit_id`). **This is the best structured quota source of the four agents.** | verified locally |
| Limit message | `You've hit your usage limit. Upgrade to Pro (…) or try again in 3 hours 2 minutes.` (0.27.0); `You've hit your usage limit. Upgrade to Pro (…), visit https://chatgpt.com/codex/settings/usage to purchase more credits or try again at Feb 23rd, 2026 9:01 PM.`; business seats: `… send a request to your admin or try again at Apr 12th, 2026 3:31 PM.` The absolute time has no zone (local time assumed: UNVERIFIED). Text for 0.156.1: pending capture. | GitHub issues [#3031](https://github.com/openai/codex/issues/3031), [#12299](https://github.com/openai/codex/issues/12299), [#16917](https://github.com/openai/codex/issues/16917) |
| Context full | Log shows `compacted` records and `event_msg/context_compacted` (22 each locally); `model_context_window` gives the size. The on-screen text and the auto-compaction threshold are UNVERIFIED. | verified locally |
| herdr detection (manifest 2026.09.23.1) | blocked: `osc_title_blocked` (`Action Required` in title), `trust_directory`, `startup_update`, `live_strong_blocker`, `weak_blocker`. working: `osc_title_working` (braille spinner), `screen_working_fallback` (timer line). skip: `transcript_viewer`. No idle rule: idle is herdr's fallback (UNVERIFIED how). | `fixtures/herdr/agent-detection/codex-2026.09.23.1.toml` |
| Gaps for coban | No rule for the usage-limit message, compaction, crash, or the `codex resume` picker. `startup_update` blocks on "Update available!", so an unattended start can stall until answered. | manifest |
| Terms (automation) | ChatGPT-plan sign-in falls under OpenAI's Terms of Use. A clause prohibiting "automatically or programmatically extract[ing] data or Output" and circumventing rate limits is reported: UNVERIFIED (openai.com and help.openai.com returned 403 to the fetch; seen only in a search summary). Codex CLI itself is Apache-2.0. | [Terms of Use](https://openai.com/policies/row-terms-of-use/) (not fetched); [help article](https://help.openai.com/en/articles/11369540-using-codex-with-your-chatgpt-plan) (not fetched) |

## Gemini CLI

| Topic | Finding | Source |
|-------|---------|--------|
| Tested version | 0.61.0. No local sessions before this study. | verified locally |
| Launch | `gemini` in the project directory. | [README](https://github.com/google-gemini/gemini-cli) |
| Resume | `gemini --resume` (latest), `gemini --resume <index>` or `<uuid>`, `/resume` in-session browser, `gemini --list-sessions`, `gemini --delete-session <n>`. | [official docs](https://github.com/google-gemini/gemini-cli/blob/main/docs/cli/session-management.md) (main branch) |
| herdr's resume command | None. herdr 0.9.1 has **no Gemini CLI integration**, so it neither reports a session id nor restores Gemini panes. | [herdr docs 0.9.1: integrations](https://github.com/herdrdev/herdr/blob/master/docs/versions/0.9.1/website/src/content/docs/integrations.mdx) |
| Where the session id comes from | Session UUID in the chat file (`sessionId`), and `--list-sessions`. coban has to obtain it itself. | [source v0.61.0](https://github.com/google-gemini/gemini-cli/blob/v0.61.0/packages/core/src/services/chatRecordingService.ts) |
| Log location | `~/.gemini/tmp/<project_hash>/chats/`; JSONL since the `.json` → `.jsonl` migration (0.61.0 writes `.jsonl`). Exact file names: pending local run. | [official docs](https://github.com/google-gemini/gemini-cli/blob/main/docs/cli/session-management.md); source v0.61.0 |
| Token fields | Per model message `tokens`: `input`, `output`, `cached`, `thoughts`, `tool`, `total` (mapped from the API's `promptTokenCount`, `candidatesTokenCount`, `cachedContentTokenCount`, `thoughtsTokenCount`, `toolUsePromptTokenCount`, `totalTokenCount`). | source v0.61.0 `chatRecordingService.ts` |
| Aggregation | Sum per message. Whether `cached` is included in `input` is UNVERIFIED. Quotas are counted in **requests** (per minute, per day), not tokens, so token sums do not predict the limit. | [official docs: quotas](https://github.com/google-gemini/gemini-cli/blob/main/docs/resources/quota-and-pricing.md) |
| Limit message | `Usage limit reached for <model>.` / `Access resets at <time>.` / `/stats model for usage details` / `/model to switch models.` The time comes from `Intl.DateTimeFormat('en-US', {hour: 'numeric', minute: '2-digit', timeZoneName: 'short'})`: a time and a short zone, **no date**. Capacity: `We are currently experiencing high demand for <model>.` After fallback: `Switched to fallback model <model>`. On-screen rendering: pending capture. | [source v0.61.0 `useQuotaAndFallback.ts`](https://github.com/google-gemini/gemini-cli/blob/v0.61.0/packages/cli/src/ui/hooks/useQuotaAndFallback.ts) |
| Context full | `/compress` replaces history with a summary; automatic compression at `model.compressionThreshold` (default `0.5` of the context). The on-screen signal is UNVERIFIED. | [official docs: commands](https://github.com/google-gemini/gemini-cli/blob/main/docs/reference/commands.md), [configuration](https://github.com/google-gemini/gemini-cli/blob/main/docs/reference/configuration.md) |
| herdr detection (manifest 2026.06.10.1) | Only two rules: blocked `apply_or_allow_change`, working `esc_cancel_working` (`esc to cancel` anywhere in the recent screen). No idle rule. | `fixtures/herdr/agent-detection/gemini-2026.06.10.1.toml` |
| Gaps for coban | Idle, questions, the quota dialog, model fallback, compression and resume are undetected. With no integration, all session and state information must come from coban. This is the weakest-covered agent. | manifest; herdr docs |
| Terms (automation) | Gemini CLI is Apache-2.0; Google service terms depend on the sign-in method. "Directly accessing the services powering Gemini CLI … using third-party software … (for example, using OpenClaw with Gemini CLI OAuth) is a violation" and may lead to suspension. coban must drive the `gemini` binary and never reuse its OAuth. | [official docs v0.61.0 `tos-privacy.md`](https://github.com/google-gemini/gemini-cli/blob/v0.61.0/docs/resources/tos-privacy.md) |

## OpenCode

| Topic | Finding | Source |
|-------|---------|--------|
| Tested version | 1.18.32. No local sessions before this study. | verified locally |
| Launch | `opencode` (TUI) in the project directory. | [official docs](https://opencode.ai/docs/) |
| Resume | `opencode --continue` / `-c` (last session), `--session` / `-s <id>`, `--fork`; `opencode session list [--format json]`. | [official docs: CLI](https://opencode.ai/docs/cli/) |
| herdr's resume command | `opencode --session <id>`, using the id from herdr's OpenCode plugin (v5, needs OpenCode ≥ 1.18.29). The plugin also reports lifecycle state (working / idle / blocked), unlike the Claude and Codex hooks. | [herdr source v0.9.1 `src/agent_resume.rs`](https://github.com/herdrdev/herdr/blob/v0.9.1/src/agent_resume.rs); herdr docs |
| Where the session id comes from | `session.id` in the database; `opencode session list`; `agent_session` in herdr with the plugin. | schema verified locally; official docs |
| Storage | SQLite, `~/.local/share/opencode/opencode.db` (`opencode db path`), WAL mode. Tables include `session`, `message`, `part`, `event`, `project`. `message.data` and `part.data` are JSON text. | verified locally |
| Token fields | `session` row totals: `tokens_input`, `tokens_output`, `tokens_reasoning`, `tokens_cache_read`, `tokens_cache_write`, `cost`, `model`. Per-message fields inside `message.data`: pending local run. `opencode stats [--days N] [--models]` prints totals. | schema verified locally; [official docs: CLI](https://opencode.ai/docs/cli/) |
| Aggregation | Read the `session` totals from a read-only SQLite connection (the file is live and in WAL mode). | verified locally (schema) |
| Limit message | Provider-dependent. OpenCode's own retry layer shows `Free limit reached`, `<name> usage limit reached. It will reset in <duration>. …` (OpenCode Go), `Provider is overloaded`, `Too Many Requests`, or the provider's message, and retries on `retry-after` headers. On-screen rendering: pending capture. | [source v1.18.32 `session/retry.ts`](https://github.com/anomalyco/opencode/blob/v1.18.32/packages/opencode/src/session/retry.ts) |
| Context full | UNVERIFIED (not researched in the docs; pending capture). | — |
| herdr detection (manifest 2026.06.10.1) | blocked `permission_required`; working `interrupt_hint_working`, `progress_bar_working`. No idle rule: with the plugin installed, the plugin is the lifecycle authority. | `fixtures/herdr/agent-detection/opencode-2026.06.10.1.toml`; herdr docs |
| Gaps for coban | Limit / retry states, context full, crash and resume are undetected on screen. | manifest |
| Terms (automation) | OpenCode is MIT-licensed. Model access follows each provider's terms; for example, Anthropic does not permit third-party apps to "offer Claude.ai login" or route requests through a user's Pro/Max plan. | [license](https://github.com/anomalyco/opencode); [Anthropic legal page](https://code.claude.com/docs/en/legal-and-compliance) |

## Findings that shape M2

1. **herdr's status enum is too small for coban.** `idle / working / blocked / done / unknown`,
   and no active manifest has a rule for usage limits, full context, crashes or resume
   pickers. coban-detect has to add these states on top of herdr's status, not replace it.
2. **The active detection rules are a moving remote catalog**, not the rules bundled in the
   herdr binary (Codex's manifest changed the day before this study). Fixtures must record
   the manifest version (`explain.json` does), and coban should not copy herdr's rules.
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
8. **Terms.** coban must only drive the unmodified official binaries that the user signed in
   to, never read or store their credentials, and treat "ordinary, individual usage" (Anthropic)
   as a constraint on how aggressively it resumes work. This deserves an ADR before M2.
9. **Pane width is not exposed by herdr 0.9.1** (`PaneInfo` has `scroll.viewport_rows` only),
   so fixtures record `cols: null`.

## herdr CLI notes

JSON output shapes of the herdr 0.9.1 commands coban uses, verified locally on 2026-09-29
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
choice. coban must not treat herdr's `idle` as "ready for a prompt" without its own check.
