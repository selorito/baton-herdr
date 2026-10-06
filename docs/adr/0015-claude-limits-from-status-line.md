---
status: accepted
date: 2026-10-06
---

# Claude's own rate limits, from a status line in the sessions baton starts

## Context and Problem Statement

Codex writes its rate limits into its rollout files, so its budget is its own figure
(ADR 0012). Claude Code writes none to its transcripts. baton therefore estimates
Claude's five-hour window from the tokens it recorded, against a configured or learned cap.
On a fresh install that estimate is `budget unknown`.

Claude Code does report its limits in one place: the JSON it sends to a status line
command. It sends `rate_limits.five_hour` and `rate_limits.seven_day`, each with
`used_percentage` (0–100) and `resets_at` (Unix seconds), for Pro and Max plans, after
the session's first response (code.claude.com/docs/en/statusline). How should baton
read them?

## Decision Drivers

- Never touch the agent's credentials or call its private endpoints (ADR 0005).
- Do not change the user's own Claude Code setup. A status line also hides most footer
  hints (`esc to interrupt`, `? for shortcuts`), which is a change people notice.
- Screen detection must keep working with the status line on screen.
- The command runs on every refresh of every session, so it must be fast and must never
  fail visibly.

## Considered Options

1. Keep the estimate.
2. Install a status line command in the user's `~/.claude/settings.json`.
3. Give a status line only to the Claude sessions baton starts, through
   `claude --settings`.

## Decision Outcome

Chosen option: **3**.

**The command.** `baton-detect statusline --log FILE` is that status line. It reads the
payload on stdin and keeps only the session id and the two windows, with the time it saw
them; paths, model and cost are dropped. It appends them to FILE (default
`~/.local/state/baton/claude-status.jsonl`, `[usage] claude_status_log`), but only when
the windows differ from the file's last line. It prints `5h 23% · 7d 54%`, or nothing
before the first report.

It is written in Rust, beside the collector, because it runs at every refresh and must
start fast. Whatever goes wrong goes to stderr, which Claude Code does not show, and it
always exits 0.

**Passing it.** While `[usage]` is on and baton-detect is found, batond appends
`--settings '{"statusLine":…}'` to Claude's own launch command and to its resume command.
A launch command configured in `[scheduler] launch_commands` is used as written. The
user's settings files are not read or written.

**Reading it.** `baton-detect usage --claude-status FILE` follows the file like the
agents' logs and emits each line as a `rate_limits` event for `claude`. It uses the
`usage-event.v1` contract, unchanged. The budget then uses Claude's latest report the way
it uses Codex's (source `reported`). Without a report it falls back to the estimate as
before.

**Detection.** With the status line, Claude's footer no longer shows `esc to interrupt`.
herdr still tells working from idle by the window title and the spinner line. This was
checked live: idle → working → idle. Claude's idle screen with the status line is recorded
in `fixtures/claude/idle/20261006T172135Z` and pinned in the golden file. The status text
is digits, `%`, `·` and the window names only, so it cannot look like a prompt, a spinner
or a limit message.

### Consequences

- Good: Claude's budget is Claude's own figure as soon as a baton session has had a
  response, including the weekly window, which the estimate never modelled.
- Good: agent choice and `/budget` use it with no change to their logic.
- Good: the user's own Claude sessions look and behave as before.
- Bad: only sessions baton starts report. After a quiet spell the latest report can be
  old. A window whose `resets_at` has passed counts as unused, as for Codex. Within a
  window, usage outside baton is seen only at baton's next session.
- Bad: a user whose own settings define a status line sees baton's in baton's sessions
  instead, because `--settings` takes precedence for those sessions.
- Bad: a configured Claude launch command gets no status line; it must add the
  `--settings` itself.
- Bad: the payload format is Claude Code's, documented but not versioned. A rename would
  make the command record nothing, and the budget would fall back to the estimate.

## Pros and Cons of the Options

### 1. Keep the estimate

- Good: nothing new.
- Bad: `budget unknown` until a limit is hit or a cap configured; the weekly window
  unknown.

### 2. A status line in the user's settings

- Good: every Claude session reports, not only baton's.
- Bad: changes the user's own setup and its footer, and must merge with or refuse an
  existing status line.

### 3. A status line in baton's sessions only

- Good: no change outside baton; nothing to install.
- Bad: reports only while baton runs Claude.
