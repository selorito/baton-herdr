# 2026-10-18: where baton's agents spend tokens

## Measured

The 19 Claude sessions baton started so far (206 API calls), from `usage_records` and
the sessions' own logs:

| | Tokens | Counted against limits |
|---|---:|---|
| cache read | 11.29M | hardly |
| cache write | 624k | yes, ~77 % of what counts |
| output | 186k | yes, ~23 % |
| input | 9k | yes |

What fills the context, by tool result: 41 full-page screenshots read with Read (by far
the most), Bash output ~71k tokens (`cat`, `wc`, `sed`, `ls`), text files read ~22k.
Every session started with a ~31k-token prompt, ~8–13k of it written anew per session.

`claude -p` in an empty directory, Claude Code 2.1.296:

| Settings | Prompt per call | Written per new session |
|---|---:|---:|
| default | 30.7k | 8.1k |
| claude.ai connectors off | 30.7k | 8.1k |
| synced skills off | 27.6k | 4.9k |
| synced and bundled skills off | 25.0k | 2.4k |
| Skill tool denied | 29.5k | (prefix changed, cache missed) |

## Done

- Lean sessions (`[scheduler] lean_sessions`, default on): the last row above, in baton's
  sessions only.
- Steps check their work without screenshots; the last step takes two at most.
- `[scheduler.models]`: a model per kind of task, unset by default.

## Looked at, not added

Tools on GitHub for fewer tokens (rtk-ai/rtk, squeez, tamp, caveman, Serena,
code-review-graph, claude-context, context-mode). On this workload they address small
shares: Bash output is ~9 % of what is written to the cache, the assistant's prose ~5 % of
its output (most output is file contents), text files read ~3 %. The code-search tools pay
off in large repositories, not in baton's small projects. They also hook into the agent's
commands or add MCP servers, so they belong in the user's agent setup, not inside baton.
Worth a measurement before any of them: rtk on `baton task add` work in a large repository.
