"""Screen-based detection of the states herdr does not report (ADR 0004, ADR 0007).

The engine is generic and pure; agent-specific rules live in ``coban.adapters``.
Its input and output are the detector's external contract: they serialise to one
JSON object per line (NDJSON) and are described by the JSON Schemas in
``schemas/``, so the engine can be replaced by another implementation without
changing callers.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import TYPE_CHECKING, Literal
from zoneinfo import ZoneInfo

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field

from coban.core.model import AgentKind, AgentState

if TYPE_CHECKING:
    from collections.abc import Callable, Sequence
    from datetime import datetime

DETECTION_CONTRACT_VERSION = 1


class DetectionRequest(BaseModel):
    """One screen to classify."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    contract: Literal[1] = 1
    agent: AgentKind
    agent_version: str | None = None
    screen: str
    # What herdr concluded, already mapped to coban's vocabulary (coban.herdr.state).
    host_state: AgentState = AgentState.UNKNOWN
    host_evidence: str | None = None
    observed_at: AwareDatetime
    # IANA zone used to read clock times printed by the agent, e.g. "resets 3:45pm".
    timezone: str = "UTC"


class DetectionResult(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    contract: Literal[1] = 1
    state: AgentState
    evidence: str
    # When a usage limit is expected to lift, if the screen says so.
    resets_at: AwareDatetime | None = Field(default=None)


type ResetParser = Callable[[re.Match[str], datetime, ZoneInfo], datetime | None]


@dataclass(frozen=True, slots=True)
class ScreenRule:
    """A pattern over the bottom of the screen that establishes a state.

    ``applies_when`` limits the rule to certain host states; ``None`` means
    always. Rules that only guess (a positive ``idle`` for an agent herdr has no
    idle rule for) should apply only when the host has nothing better, so that
    herdr's positive ``working`` or ``blocked`` signals are never overridden.
    """

    rule_id: str
    state: AgentState
    pattern: re.Pattern[str]
    bottom_lines: int = 30
    applies_when: frozenset[AgentState] | None = None
    reset_parser: ResetParser | None = None


def detect(request: DetectionRequest, rules: Sequence[ScreenRule]) -> DetectionResult:
    """First matching rule wins; otherwise the host's conclusion stands."""
    zone = ZoneInfo(request.timezone)
    for rule in rules:
        if rule.applies_when is not None and request.host_state not in rule.applies_when:
            continue
        match = rule.pattern.search(bottom(request.screen, rule.bottom_lines))
        if match is None:
            continue
        resets_at = (
            rule.reset_parser(match, request.observed_at, zone) if rule.reset_parser else None
        )
        return DetectionResult(
            state=rule.state, evidence=f"coban:{rule.rule_id}", resets_at=resets_at
        )
    return DetectionResult(
        state=request.host_state, evidence=request.host_evidence or "host:no-evidence"
    )


def bottom(screen: str, lines: int) -> str:
    """The last ``lines`` non-empty lines, right-trimmed, joined by newlines."""
    kept = [line.rstrip() for line in screen.splitlines() if line.strip()]
    return "\n".join(kept[-lines:])
