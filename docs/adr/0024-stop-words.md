---
status: accepted
date: 2026-11-14
---

# Stop words: "dur" cancels the work under way, without a button

## Context and Problem Statement

The assistant (ADR 0021) turns "cancel the cafe task" into a card, and only the owner's
press on it acts. That is right for a sentence a small model has to interpret, but too
slow when the operator wants work to stop now: they type "dur", and wait for a model that
may be loading, busy with another message, or misreading a one-word message. The operator
asked for a stop word that stops the task outright.

ADR 0009 and 0021 say free text never acts without a button. Can one word act at once,
without opening a way for any other text to?

## Decision Drivers

- Stopping must work when the model is slow, busy, wrong or switched off.
- Only a message that is plainly a stop acts; anything longer goes to the assistant.
- It stops by the same function `/cancel` uses, with the same record.
- Stopping is the direction that is safe to get wrong: nothing new is started, and what a
  task did is still in its directory.

## Considered Options

1. Keep cards for every cancel.
2. Let the model's `cancel` intent act without a card.
3. A closed list of stop words, read by baton before the assistant, acting at once.

## Decision Outcome

Chosen option: **3** (`assistant/stop.py`, `OperatorBot.stop`).

- **What is a stop.** At most six words, at least one of them a stop word (`dur`,
  `durdur`, `iptal`, `vazgeç`, `bırak`, `yeter`, `kes`, `stop`, `cancel`, `abort`,
  `halt`), every other word a filler ("hemen", "lütfen", "it", "now"), "all"
  ("hepsini", "everything") or a task or project id. Case and punctuation do not count,
  Turkish casing included. "kafe görevini durdur" is a sentence about something: it goes
  to the assistant, which proposes a card (ADR 0021).
- **Read first.** The bot checks for a stop before the assistant and before the help
  text, so it works with no assistant configured and does not wait on the model.
- **What it stops.** The ids written with it, else every task with a live attempt
  (running, waiting for a person, interrupted). A project's step stands for its project,
  so its later steps do not start. Queued tasks with no attempt are left alone.
- **How.** One target, "all", or ids: cancelled at once through `OperatorBot._cancel`,
  the `/cancel` path (`cancelled by telegram:<owner>`, panes closed). Several targets and
  no "all": one button per target (`task:cancel:<id>`, the existing cancel button) and
  "Stop them all".
- **Language.** The answer is in Turkish when the stop was.

### Consequences

- Good: "dur" stops within a second, whatever the model is doing.
- Good: no new event type and no new way to start anything.
- Bad: a stop word sent by mistake cancels a task. Its directory keeps what was done,
  and the request can be given again.
- Neutral: the model's `cancel` intent keeps its card: a sentence is interpreted, a stop
  word is not.
