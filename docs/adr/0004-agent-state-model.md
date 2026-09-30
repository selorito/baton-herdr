---
status: accepted
date: 2026-09-30
---

# coban's agent state model on top of herdr's status

## Context and Problem Statement

herdr reports one of `idle`, `working`, `blocked`, `done`, `unknown` per pane. The H0 study
(`docs/research/agents.md`) showed that this is not enough to decide what coban should do:

- `idle` is also herdr's fallback for a known agent whose screen matches no rule. Every
  first-run dialog and resume picker we captured was reported as `idle`.
- `done` depends on whether a human is looking at the pane.
- There is no status for usage limits, full context, a crashed agent, or an agent that asked
  a question in plain text and ended its turn.
- A crash looks like a pane with no agent (`agent: null`, `unknown`), which is also what an
  ordinary shell looks like.

Which states does coban reason about, and where do they come from?

## Decision Drivers

- Never send a prompt to a screen that is not a prompt.
- Recovery needs to know *why* an agent stopped, not only that it stopped.
- herdr's detection rules change independently of coban (remote catalog).

## Considered Options

1. Use herdr's five statuses as coban's state.
2. Replace herdr's detection with coban's own for every state.
3. Keep herdr's status as one input and define a richer coban state derived from several
   inputs.

## Decision Outcome

Chosen option: **3**. `coban.core` defines `AgentState`:

| State | Meaning |
|-------|---------|
| `working` | a turn is running |
| `idle` | the agent is at its prompt and can take input |
| `blocked_permission` | waiting for an approve / deny decision |
| `blocked_question` | waiting for an answer (dialog, or plain text with the question contract) |
| `blocked_other` | blocked, kind not yet classified (trust dialog, update prompt, picker, …) |
| `rate_limited` | usage limit reached; may carry a reset time |
| `context_full` | context window exhausted and not recoverable by the agent itself |
| `crashed` | the agent process is gone while an attempt was active |
| `unknown` | none of the above could be established |

Rules for deriving it (implemented outside `core`, in `herdr/`, `adapters/` and coban-detect):

- herdr `working` and `blocked` are accepted; `blocked` is refined to a `blocked_*` kind by
  the matched rule (for example Claude's `bash_permission_prompt` vs `live_blocked_form`) or by
  coban-detect.
- herdr `idle` counts as `idle` **only** when `agent explain` names a matched rule or an
  integration reports lifecycle. A rule-less `idle`
  (`fallback_reason: default_known_agent_idle_fallback`) is `unknown`.
- herdr `done` means "a turn ended"; it maps through the `idle` rule above. Completion is
  inferred from the `working → idle|done` transition, never from `done` alone.
- `rate_limited`, `context_full` and `crashed` are never produced by herdr; they come from
  coban-detect, agent usage logs, and `pane process-info`.
- Plain-text questions are recognised by a contract in the task instructions (a final line
  `[[COBAN:QUESTION]]`), with classification of the final message as the fallback. The
  contract is a convention, not a guarantee.

Every observation is recorded as an event with its source and evidence, so a wrong
classification can be traced and replayed.

### Consequences

- Good: coban acts only on positive signals; ambiguous screens become `unknown` and are
  escalated instead of prompted.
- Good: recovery can branch on the reason an attempt stopped.
- Bad: coban must maintain its own detection for the states herdr lacks, per agent and per
  agent version.
- Bad: two classifiers can disagree; the event log keeps both so the disagreement is visible.

## Pros and Cons of the Options

### herdr's statuses only

- Good: nothing to build.
- Bad: prompts would be typed into dialogs, and limits and crashes would be invisible.

### Replace herdr's detection

- Good: one classifier.
- Bad: duplicates maintained work for states herdr already detects well, and herdr's
  integrations (OpenCode plugin) are more reliable than screen rules.
