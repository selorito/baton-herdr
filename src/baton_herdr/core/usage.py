"""The usage contract: NDJSON lines from ``baton-detect usage`` (ADR 0011).

One JSON object per line, told apart by ``kind``:

- ``usage``: tokens one agent response used, in one shared vocabulary;
- ``rate_limits``: an agent's own report of its usage windows (Codex).

These models are the contract; ``schemas/usage-event.v1.json`` is generated from
them and the Rust implementation is tested against both.

Token vocabulary, the same for every agent:

- ``input``: input tokens not read from the prompt cache;
- ``cache_read`` / ``cache_write``: input tokens read from or written to the cache;
- ``output``: output tokens, reasoning included;
- ``reasoning``: the part of ``output`` spent on reasoning, when the agent reports it.
"""

from __future__ import annotations

from typing import Annotated, Literal

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, NonNegativeInt, TypeAdapter

type UsageAgent = Literal["claude", "codex", "opencode"]


class _Contract(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")


class UsageTokens(_Contract):
    input: NonNegativeInt
    output: NonNegativeInt
    cache_read: NonNegativeInt
    cache_write: NonNegativeInt
    reasoning: NonNegativeInt | None = None


class UsageRecord(_Contract):
    """Tokens one agent response used."""

    kind: Literal["usage"] = "usage"
    agent: UsageAgent
    session_id: str = Field(min_length=1)
    # Stable across re-reads: the same response always gets the same id.
    record_id: str = Field(min_length=1)
    at: AwareDatetime
    model: str | None = None
    tokens: UsageTokens


class RateLimitWindow(_Contract):
    name: str = Field(min_length=1)  # e.g. "primary" (5 hours) or "secondary" (7 days)
    window_minutes: int = Field(gt=0)
    used_percent: float = Field(ge=0)
    resets_at: AwareDatetime | None = None


class RateLimitObservation(_Contract):
    """An agent's own report of its usage windows, sent when it changes."""

    kind: Literal["rate_limits"] = "rate_limits"
    agent: UsageAgent
    session_id: str = Field(min_length=1)
    at: AwareDatetime
    windows: tuple[RateLimitWindow, ...] = Field(min_length=1)


type UsageEvent = Annotated[UsageRecord | RateLimitObservation, Field(discriminator="kind")]

USAGE_EVENT: TypeAdapter[UsageEvent] = TypeAdapter(UsageEvent)
