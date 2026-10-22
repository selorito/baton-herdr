---
status: accepted
date: 2026-10-22
---

# Task tiers: economy runs the same agents on a cheaper model, behind a provider backend

## Context and Problem Statement

Every task runs on an agent's default model, or on the model `[scheduler.models]` names for
its kind (`plan`, `step`, `merge`, `task`). Much of a project's work is routine: a page, a
test, a rename. It does not need the strongest model. Running it on a cheaper one (Claude
Code's `--model haiku`, a smaller Codex model with low reasoning effort) costs less of the
account's windows and is often faster. When the cheaper model fails, the task should move
up rather than wait for a person.

A gateway in front of many providers (OmniRoute) was considered and set aside. It needs a
server, keys, and changes to the agents' own configuration, and its usage would have to be
told apart from the subscription's.

How should a task's tier be chosen and applied, and how does a failed economy attempt move
up, so that another way to run an agent (a gateway, a local model) can be added later
without changing the scheduler again?

## Decision Drivers

- **Nothing changes for whoever does not opt in.** With no `[tiers]` section, every task
  runs exactly as today.
- **Agent differences stay in `adapters/`.** A tier names a model and flags; the adapter
  turns a model into arguments (`model_args`), as `[scheduler.models]` already does.
- **The user's own configuration is never written.** No `~/.claude` or `~/.codex` files
  are changed; everything is in the command baton starts, or in the pane's environment.
- **The log stays the source of truth** (ADR 0002). Tier and escalation are events.
- **Budget stays a preference, not a gate** (ADR 0012). Economy on the same account
  shares that account's limits (ADR 0019).

## Considered Options

1. A second set of agents ("claude-economy") in `[scheduler] agents`.
2. A per-kind model only, as `[scheduler.models]` does now, with no tier and no escalation.
3. A tier per task, a launch profile per (agent, tier, kind) from a provider backend, and
   escalation in the recovery policy.

## Decision Outcome

Chosen option: **3**.

Option 1 would make one account look like two agents. Their limits would be counted
apart, and a limit on one would not set the other aside.

### Interface (`core/routing.py`)

```python
class Tier(StrEnum):
    PREMIUM = "premium"
    ECONOMY = "economy"


@dataclass(frozen=True, slots=True)
class LaunchProfile:
    backend: str  # "direct"; later e.g. "gateway", "local"
    model: str | None = None  # turned into arguments by the adapter's model_args
    args: tuple[str, ...] = ()  # extra flags, as given
    env: Mapping[str, str] = {}  # for the pane; empty for direct
    own_budget: bool = False  # True: not counted against the agent's account


class ProviderBackend(Protocol):
    name: str

    def profile(self, agent: AgentKind, tier: Tier, role: str) -> LaunchProfile | None:
        """None: this backend cannot run the agent in this tier."""

    async def health(self) -> BackendHealth: ...
```

- **`DirectBackend`** (`scheduler/backend.py`) starts the agents as installed.
  - Premium comes from `[scheduler.models]`, unchanged.
  - Economy comes from `[tiers.economy]`, per agent: `model` and `args`. An agent without
    an economy entry has no economy profile.
  - Health is always up.
- **The pane's environment.** `PaneHost.open_pane` gains `env` only when the first backend
  that needs it does. Until then the runner refuses a profile with an environment.
- **Arguments.** Launch and resume both take the profile's arguments, so a resumed
  economy session stays economy.

### Tiers

```toml
[tiers]
roles = { step = "economy" }   # tier per kind of task; unnamed kinds: premium
economy_fallback = "premium"   # no agent has an economy profile: "premium" | "wait"

[tiers.economy]
claude = { model = "haiku" }
# codex = { model = "<a smaller Codex model>", args = ["-c", "model_reasoning_effort=low"] }

[tiers.escalation]
after = 1                                    # economy failures before premium
on = ["stalled", "crashed", "failed_start"]
economy_stall_minutes = 10
```

- **Defaults.** There are no default roles, so everything is premium. The example
  configuration names no Codex model; Codex's model names change, and a wrong one fails to
  start.
- **On the task.** A task's tier is `task.created.tier`; when it is absent, the tier comes
  from its kind through `roles`. `baton task add --tier` sets it. Each attempt records the
  tier and backend it ran on (`attempt.started.tier`, `.backend`). All of these fields are
  optional, so old logs read as premium on `direct`.
- **Agent choice.** For an economy task, `choose_agent` puts agents with an economy profile
  first, in preference order. Without one, `economy_fallback` applies, and the reason says
  so ("economy: claude (haiku)", "no economy profile: claude, premium").

### Escalation

- An economy attempt interrupted for a reason in `escalation.on` counts as an economy
  failure. After `after` of them, `plan_recovery` returns **`ESCALATE`**.
- The next attempt runs on the same agent, premium, as a fresh session with the hand-off
  prompt. A stalled economy session is not resumed: its context is what got stuck. A crash
  is resumed in its own tier first, as today, within `max_failure_resumes`.
- The step is recorded as `task.escalated`, with `from`, `to`, `reason` and the failures
  counted. The notice says so ("⬆ stalled on haiku, continuing on the default model").
- An escalated task does not go back down by itself.
- `rate_limited` and `signed_out` never escalate. The account is limited or signed out in
  both tiers, so the hand-off of ADR 0005 and 1.2 applies.

### Phases

| Phase | What |
|---|---|
| 1 | The interface and `DirectBackend`, premium only. The runner builds commands from a profile; behaviour unchanged. |
| 2 | `[tiers]`, the tier on tasks and attempts, the economy preference in agent choice, `--tier`. |
| 3 | Escalation. |
| 4 | Telegram and CLI: `/tier t-… economy\|premium`, `#economy` on `/new`, the tier in `/status`. |
| 5 | Documentation. |

A Gemini CLI adapter, for Gemini's own free tier, is a separate decision that revisits
ADR 0008. It is not part of this one.

### Consequences

- Good: routine steps can run on a cheaper model with one line of configuration. A failure
  costs one economy attempt, then premium.
- Good: a gateway or a local model is one more `ProviderBackend`. The scheduler, recovery
  and the log do not change for it.
- Bad: economy saves window share, not a separate quota. With one account, a long economy
  run still brings the limit nearer.
- Bad: baton cannot tell a poor result from a good one. A cheap model that finishes badly
  is not escalated; a project's check step is where that shows.
