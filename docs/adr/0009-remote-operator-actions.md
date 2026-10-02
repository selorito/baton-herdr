---
status: accepted
date: 2026-10-03
---

# Remote operator actions: one owner, actions bound to a blocker, no free-form input

## Context and Problem Statement

coban types into terminals that run coding agents with access to the user's code and shell.
The MVP lets the operator act on a waiting task from Telegram: approve or deny a permission
prompt, answer an agent's question, see the status. Anything that can reach that bot can
reach those terminals, and a button pressed late can land on a different prompt than the
one it was shown for. What may a remote action do, and how is it checked?

## Decision Drivers

- A Telegram account or chat other than the owner's must not be able to type into a pane.
- An action must apply to the prompt the operator saw, or to nothing.
- ADR 0005: sign-in, folder trust and hook review are always decided by a person at the
  terminal; ADR 0006: input goes only to a pane verified to host the attempt.

## Decision Outcome

1. **Owner lock.** The bot handles an update only when it comes from the configured
   `chat_id` **and** from the configured `owner_id` (a Telegram user id). Without
   `owner_id` the bot only sends notices. Everything else is ignored without a reply.
2. **Actions are bound to a blocker.** A notice that asks for a decision names the blocker:
   the position (`seq`) of the `attempt.state_observed` event that recorded it. A button
   or reply carries that `seq`. An action is carried out only when:
   - the task's live attempt is active and its newest observation is still that event;
   - no operator action has been recorded for that blocker yet (each blocker is answered
     at most once);
   - the action fits the blocker (below);
   - the pane is verified to host the attempt (ADR 0006).
   Otherwise the operator is told why and nothing is sent.
3. **A closed set of actions.**
   - `approve` / `deny`: only for `blocked_permission`, and only with keys the agent's
     adapter declares for that exact detection rule (`permission_keys`). A blocker
     without declared keys is answered at the terminal.
   - `answer`: free text, only for a question the agent asked at its prompt (the
     `[[COBAN:END status=question]]` mark). It is sent as one prompt, the way coban sends
     a task. It is never sent into a dialog, a shell or a pane without a verified agent.
   - `status`: read only.
   There is no command that types arbitrary text or keys into a pane.
4. **Recorded.** Every carried-out action is an `operator.acted` event: action, blocker,
   Telegram user id, and the answer's length (not its text, as with prompts).

### Consequences

- Good: a leaked bot token alone cannot drive a pane; a stale button cannot approve a
  newer, different prompt.
- Good: the same checks serve the CLI (`coban approve|deny|answer`), so they are tested
  without Telegram.
- Bad: agents whose approval keys are not verified (OpenCode for now) still need the
  terminal for permission prompts.
- Bad: one owner only; sharing a bot with a team needs another ADR.
