---
status: accepted
date: 2026-10-22
amended: 2026-11-10
---

# A natural-language assistant in Telegram: a local model that proposes, the operator who starts

## Context and Problem Statement

The Telegram bot (ADR 0009) understands commands (`/new`, `/status`, `/cancel`, `/budget`),
buttons, and replies to a question. Anything else gets "That message went to no agent" and
the help text. The operator has to remember the commands and the ids `t-…` and `p-…`. A
question such as "why is t-42 stuck?" needs `/status`, and then reading the log on the
machine.

ADR 0009 decided "no free-form input": an operator's action is bound to a blocker, and text
reaches an agent only as the answer to its question. Can the bot understand plain messages,
whether a question about the work, a request for new work or small talk, without opening
another way for text to act on the machine?

## Decision Drivers

- **Interpreter, not agent.** The model only interprets a message. It has no file system,
  shell or herdr access, and nothing it writes is acted on, except a proposal that passes
  a schema and is then confirmed with a button.
- **One code path.** Work asked for in words is made by the same function as `/new`
  (`projects.flow.start_project`, ADR 0016). That function checks the request and makes the
  directory. Budget is applied at agent choice (ADR 0012) and is not checked again here.
- **Plan before code.** A project from words is a `/new` project: an agent plans it, and
  nothing is coded before the operator presses Start under the plan (ADR 0016).
- **Optional and local.** Without `[assistant]`, the bot behaves as today. A local model
  (Ollama) costs no quota and sends nothing off the machine. A hosted model can be
  configured later through the same interface.
- **Shared machine.** The model runs beside the agents (ADR 0017). One request at a time, a
  timeout, and the bot never waits on it for anything else.
- **The log stays the source of truth** (ADR 0002). Conversations and unconfirmed proposals
  are not domain state.

## Considered Options

1. Keep commands only. Improve `/help` and add `/why t-…`.
2. A hosted model with tool use, which calls `start_project` and reads the log itself.
3. A local model behind an interface. It classifies each message into a closed set of
   intents with a JSON schema. baton validates the result and acts only through existing
   functions, after a button press.

## Decision Outcome

Chosen option: **3**.

### Interface

`core/assistant.py` holds pure types and a protocol:

```python
class AssistantBackend(Protocol):
    name: str

    async def interpret(self, turn: AssistantTurn) -> str:
        """The model's raw JSON text for this turn; raises AssistantUnavailableError."""

    async def health(self) -> AssistantHealth: ...
```

- **Input.** `AssistantTurn` holds the fixed instructions, the last few messages of the
  conversation, the operator's new message as a quoted data field, and, when asked for, a
  status digest.
- **Output.** The backend returns text. Validating it is baton's job (`parse_intent`), so
  every backend is held to the same schema.
- **Implementations.** The first is `OllamaAssistant`: `POST /api/chat` with
  `stream: false` and `format: <the intent JSON Schema>`. Ollama documents that the
  response then matches the schema, and that the schema should also be stated in the
  prompt. A hosted backend (for example a small Claude model) is added later as another
  implementation selected in `[assistant] backend`.
- **The schema the model sees** leaves out `maxLength`. Ollama 0.34 turns the schema into
  a grammar and refuses a long `maxLength` (2000 characters) as one it cannot parse (seen
  in A1). `parse_intent` still checks every length against the full schema.
- **Configuration.** The model name and address come only from configuration. There is no
  default model. The example configuration shows `qwen3.5:4b`: measured beside `ornith-1.5:9b`, it
  reads as well and leaves 3.9 GB free instead of 1.1 GB on a 15 GB machine.

### Intents (v1)

`chat`, `status` and `new_project`. `cancel` and `set_tier` were left for later; they are
added by the amendment below.

A `new_project` proposal names its target as `{"kind": "new"}` or
`{"kind": "existing", "path": "…"}`. v1 accepted only `new`. Since the amendment of
2026-11-10, an `existing` target is accepted without a schema change: see "Work in an
existing directory" below.

- The model's output is parsed with pydantic. Output that does not parse, fields that do
  not fit the intent, or an unknown intent are all treated as `chat`, with the reply "I
  did not understand; try /new or /status". The raw output is logged at debug level and
  never shown to the operator.
- `confidence` below `[assistant] min_confidence` is treated as `chat`.
- **`status`:** baton builds the digest itself.
  - A "why" question (`status.why = true`: why stuck, why handed off, why failed) gets the
    digest explained by the model in a second call. The model is told to use only the
    digest, to say "not in the record" for anything the digest does not hold, and to cite
    the event sequence numbers and attempt ids it relied on. The digest carries those ids.
    A citation that is not in the digest is dropped from the answer.
  - Every other status question gets the digest rendered by baton in a fixed form, with no
    second call.
- **`new_project`:** a confirmation card when the request is complete enough. Otherwise
  the model's question, and no card.

### Confirmation card

- The card shows the request as it will be passed to `/new`, where the project will be
  made (`[projects] root`, with the directory named by baton, ADR 0016), the agent a task
  would go to now and why (`choose_agent`, as `baton budget` shows), and the tier when ADR
  0020 is in place.
- It has three buttons: **Start**, **Edit** and **Cancel**.
- Cards live in memory, keyed by a short random id carried in the button data (Telegram
  allows 64 bytes). Each card has a lifetime (`card_ttl_minutes`) and a state. A press on
  a card in a final state, or on a card unknown after a restart, answers with that state
  ("already started: p-…", "expired") and never acts twice.
- The press is decided under a lock per card, so two quick presses start one project.

### Language

`[assistant] language = "auto" | "tr" | "en"`. With `auto`, the assistant answers in the
language of the operator's message. The model is told to, and the card's templates follow
the language of the turn. With `tr` or `en`, both use that language. The bot's other
messages (notices, commands) stay in English in v1.

### Safety

- **Who.** The same check as every update (ADR 0009): the configured chat and owner only.
- **Message as data.** The operator's message reaches the model as data to classify,
  inside a delimited field after fixed instructions. Whatever the model writes, baton does
  only what the schema allows. Starting work also needs a button press by the owner.
- **The digest.** The digest is built from the projection, and only from task ids,
  titles, statuses, agents, attempt counts, interruption reasons, blocker kinds, the rule
  names that matched (`baton:claude_trust_folder`) and times. It never includes
  instructions, screens, notices' blocks, environment, paths outside the projects root,
  settings or tokens.
- **Paths.** A new project's directory is made by baton. The model names a path only as
  the operator wrote it, for work in an existing directory, and baton alone decides whether
  work may go there (below).

### Speed (measured in A2, `docs/devlog/2026-10-22-assistant-on-ollama.md`)

- The model is asked for no hidden chain of thought (`think: false`, only for a model whose
  `/api/show` lists `thinking`) and for the same reading each time (`temperature: 0`).
- The instructions describe the answer's shape in words. The schema goes to Ollama as the
  answer's grammar (`format`) only: in the prompt as well, it doubled the prompt.
- **Status questions that need no model** are read by baton itself: a message that names
  tasks or projects and asks how they stand ("t-1a2b3c4d ne durumda?") or why, with no word
  asking for work, and a short "what is running?". A plain status answer is then instant;
  a "why" takes one call (the explanation) instead of two.

### Resources

- One request at a time (a semaphore), within `timeout_seconds`. A message that arrives
  while one is being answered gets "busy, ask again in a moment" and is not queued.
- While the model runs, the bot shows Telegram's typing indicator. On timeout it answers
  "The assistant did not answer; /new and /status still work."
- Commands, buttons and replies are not handled by the assistant, and do not wait for it.
- `keep_alive` is configurable, so the model can be unloaded between messages and leave
  its memory to the agents.

### What changes from ADR 0009

Free-form text now reaches a model, but not an agent and not a pane. Its only effects are a
reply in the chat, or a card whose Start calls `start_project`, the same function as
`/new`. Text still reaches an agent only as an answer to its question.

### Consequences

- Good: the bot can be used in words, in the operator's language, and "why is it stuck?"
  is answered from the log without opening the machine.
- Good: no new way to create work, and no new event types. A project started from a card
  is an ordinary `/new` project.
- Bad: a small local model misreads some messages. They fall back to `chat`, which costs
  the operator a retry or a command, never an unwanted action.
- Bad: a 7–9B model on the CPU takes seconds per reply and holds several GB of memory
  while loaded, next to the agents.
- Bad: conversations and cards are lost on restart, so open cards expire. This is accepted:
  nothing has been started from them.
- Measured in A2's live test: whether replies fit the schema, reply time, and the model's
  memory, both on an idle machine and while agents run.
- Not in v1: more than one allowed user, voice messages. Tasks in an existing directory
  from words, `cancel` and `set_tier` came with the amendments below.

## Amendment (2026-11-10): `cancel` and `set_tier`

Two intents join the closed set, under the same rule: the model interprets, baton acts
only after the owner presses a button on a card.

- **Schema.** `cancel: {task}` and `set_tier: {task, tier}`, `tier` being `economy` or
  `premium`. `task` is a `TaskRef`: the `id` the message writes (`t-…`, or `p-…` to cancel
  a project), or, when it writes none, the `words` it names the task by ("the cafe task":
  `cafe`). As for the other intents, the object named like the intent is set, and only it.
- **baton finds the task, not the model** (`assistant/targets.py`, `find_target`). An id
  counts only when it is in the operator's message or one of their earlier messages in
  the conversation; one the model wrote by itself is set aside. Words are matched against
  the titles and requests of open tasks. A project id to `set_tier` asks which of its
  open tasks is meant (tiers are per task, ADR 0020).
- **Not plain, then a question.** No match, more than one, an id the log does not have, or
  nothing named: the operator is asked, with the candidates' ids when there are some, and
  no card is shown. The next message, if it is an id alone, answers the question without
  a model call. A task already closed gets "already cancelled; nothing to change".
- **The card** names the task (id, title, status, agent) and what will happen: for a
  cancel, that it is dropped and not tried again, and its pane closed when it has one;
  for a project, every open task that goes with it; for a tier, the new tier, the current
  one when known, and that it applies from the next attempt. Two buttons: **Cancel it** /
  **Keep it**, or **Change** / **Keep**. There is no Edit.
- **One code path.** The card holds its command's arguments. Start calls what `/cancel`
  and `/tier` call in the bot (`scheduler.operator.cancel`, `projects.flow.cancel_project`,
  `scheduler.operator.set_tier`), once per card under the card's lock. Those functions
  check again at the press, so a task closed after the card was shown is refused with
  their own words. No new event types.
- **Plain status.** A message with a word asking for a cancel or a tier (`iptal`, `cancel`,
  `stop`, `economy`, `ucuz`, …) is never read as a plain status question.
- Without cards (no `CardStore`), the answer is the command that would do it, as for
  projects.

## Amendment 2026-11-10: work in an existing directory

"~/dev/baton-projects/wsl1-smoke içinde calc.py'ye bölme ekle" names a directory that
exists. The model answers `new_project` with `target = {"kind": "existing", "path": …}`,
the directory exactly as the operator wrote it, or a project id (`p-1a2b3c`) when the
operator names a project. The schema is the one of v1.

- **baton decides where.** `assistant/places.py`, `Places.find`, resolves the path through
  `core.assistant.Directories` (`~` expanded, links followed, so a link inside a trusted
  directory is judged by where it leads) and offers it only when it is an existing
  directory and either inside the policy's `trusted_dirs` or inside a project baton made
  (a plan task's directory in the log). A project id stands for that project's directory.
- **Anything else is refused with the reason**, and no card is shown: a relative path
  ("write the full path"), a directory that does not exist ("baton makes, moves and
  deletes no directories"), one outside `trusted_dirs` and every project ("add it to
  trusted_dirs if you trust it"), a project id the log does not hold. With the policy off
  there are no `trusted_dirs`, so only baton's projects are offered.
- **Never create, move or delete.** `LocalDirectories` only resolves and looks. Nothing
  in this path makes a directory, so a missing one is a refusal, not a new project.
- **The card** shows the directory, why it is allowed (`trusted_dirs`, or the project's
  id), a title (the request's first sentence, at most 60 characters), the instructions
  (the request), the agent a task would go to now, and the tier (`[tiers] roles.task`,
  or a tag in the request). There is no plan step: once started, the agent works in that
  directory, as with `baton task add`.
- **Start** checks the directory again, since it may have gone or the policy changed while
  the card waited, and then calls `scheduler.operator.add_task`, the function `baton task
  add` calls (moved there from `daemon` so the bot can call it). It runs once per card,
  under the card's lock, as for a project. A refusal at Start is the card's final answer.
- **Edit** gives the model the card's directory with the request, so the revision keeps it
  unless the change names another, and the new card is checked again.
- **The task** is an ordinary `task.created`: no project, no new event type. Whether the
  policy approves tests and builds there is decided as for any task (ADR 0013).
