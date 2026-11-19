# 2026-11-10: cancel and tier in words

ADR 0021 left `cancel` and `set_tier` for later. They are now intents, under the
assistant's one rule: the model interprets, nothing happens before the owner presses a
card's button. The ADR is amended.

## What was built

- **Schema** (`core/assistant.py`). `cancel: {task}` and `set_tier: {task, tier}`. A
  `TaskRef` names the task by `id` or, when the message writes none, by the operator's
  `words` ("cancel the cafe task": `cafe`). The model is told never to make up an id.
- **Finding the task** (`assistant/targets.py`, `find_target`, pure). baton decides which
  task is meant: an id only if the operator wrote it (in this message or an earlier one),
  words against the titles and requests of open tasks. No match, several, an unknown id or
  nothing named: a question listing the candidates, and no card. The id alone, sent in
  reply, is read without the model.
- **Cards** (`assistant/cards.py`). A card has a kind (project, cancel, tier) and holds
  what its command is given. Start hands the card to the bot, which runs the `/cancel` or
  `/tier` handler itself (`OperatorBot._cancel`, `_tier`, which now also say whether they
  did it). So a press goes through `scheduler.operator.cancel` / `set_tier` /
  `projects.flow.cancel_project`, exactly as the commands do, once per card. Edit is only
  on project cards.
- `plain_status` no longer takes "is t-… done? then stop it" for a status question.

## Tests

`tests/integration/test_assistant_actions.py`, on the fake backend: the three messages of
the task ("t-1a2b3c4d'yi iptal et", "cancel the cafe task", "t-1a2b3c4d ucuz modelde
çalışsın") each give a card naming the task and what will happen, and nothing changes
until the press; ambiguous and unknown names, an unknown id and an id the model made up
each get a question; a closed task gets no card; a task closed after its card was shown is
refused by the command; Keep and a second press change nothing; a project is cancelled
whole from its card. Unit tests hold the schema.

## Not done

- The repository has no file of assistant evaluation messages to extend: A2's twelve
  messages are only described in `2026-10-22-assistant-on-ollama.md`. The new intents
  have not been measured on Ollama (`qwen3.5:4b`); that needs a live run.
- Matching words is plain substring matching on every remaining word. A Turkish name with
  a suffix ("kafeyi") does not match "kafe" unless the model passes the stem, as it is
  told to; it then gets a question, not a wrong card.
