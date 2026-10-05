"""The detector's contract (ADR 0004, ADR 0011): one screen in, one state out.

``baton-detect classify`` answers these over NDJSON, one JSON object per line, as
described by the JSON Schemas in ``schemas/``. The rules themselves are the Rust
classifier's (``crates/baton-detect/rules/``).
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Literal

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field

from baton_herdr.core.model import AgentKind, AgentState

if TYPE_CHECKING:
    from collections.abc import Sequence

DETECTION_CONTRACT_VERSION = 1


class DetectionRequest(BaseModel):
    """One screen to classify."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    contract: Literal[1] = 1
    agent: AgentKind
    agent_version: str | None = None
    screen: str
    # What herdr concluded, already mapped to baton's vocabulary (baton_herdr.herdr.state).
    host_state: AgentState = AgentState.UNKNOWN
    host_evidence: str | None = None
    # False when the host sees no agent process in the pane. A dead full-screen agent
    # can leave its last frame under the shell prompt, so the screen alone cannot tell.
    agent_running: bool = True
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


def one_line_summary(lines: Sequence[str], *, limit: int = 500) -> str | None:
    """Join the non-empty ``lines`` with " · ", cut to ``limit`` characters."""
    parts = [line.strip() for line in lines if line.strip()]
    if not parts:
        return None
    text = " · ".join(parts)
    return text if len(text) <= limit else text[: limit - 1] + "…"
