"""Turn herdr's pane status into baton's :class:`AgentState` (ADR 0004).

herdr answers with ``idle`` whenever a known agent shows a screen that no rule
matches, so a rule-less ``idle`` is not evidence of anything. This module keeps
herdr's positive signals and downgrades the fallback to ``unknown``. Refining
``blocked`` into permission / question, and recognising limits or crashes, is
left to the adapters and the detector.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from baton_herdr.core.model import AgentState

if TYPE_CHECKING:
    from collections.abc import Mapping

IDLE_FALLBACK = "default_known_agent_idle_fallback"


def derive_state(
    agent_status: str | None, explain: Mapping[str, Any] | None
) -> tuple[AgentState, str]:
    """Return the state and the evidence for it.

    ``agent_status`` is ``PaneInfo.agent_status``; ``explain`` is the
    ``agent.explain`` payload, or ``None`` when herdr sees no agent in the pane.
    """
    if explain is None:
        return AgentState.UNKNOWN, "herdr:no-agent"

    rule = (explain.get("matched_rule") or {}).get("id")
    basis = f"herdr:rule:{rule}" if rule else "herdr:reported"
    # explain classifies the screen now. When it names a rule, its state is fresher
    # than the pane's status, which can lag (seen live: pane "idle" while the rule
    # was Codex's working title). Without a rule the status may come from an
    # integration's report, so the pane's status stands; "done" ("a turn ended,
    # not yet looked at") is always resolved through the screen.
    screen_decides = bool(rule) or agent_status in {None, "done"}
    status = explain.get("state") if screen_decides else agent_status

    if status == "working":
        return AgentState.WORKING, basis
    if status == "blocked":
        return AgentState.BLOCKED_OTHER, basis
    if status == "idle":
        if rule is None and explain.get("fallback_reason") == IDLE_FALLBACK:
            return AgentState.UNKNOWN, "herdr:idle-fallback"
        return AgentState.IDLE, basis
    return AgentState.UNKNOWN, f"herdr:status:{status}"
