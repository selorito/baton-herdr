"""Per-agent adapters: Claude Code, Codex CLI (OpenCode next; Gemini CLI in v1.1).

Agent-specific differences live only in this package, one module per agent.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from coban.adapters.claude import ClaudeAdapter
from coban.adapters.codex import CodexAdapter

if TYPE_CHECKING:
    from collections.abc import Mapping

    from coban.core.agents import AgentAdapter
    from coban.core.model import AgentKind

ADAPTERS: Mapping[AgentKind, AgentAdapter] = {
    adapter.kind: adapter for adapter in (ClaudeAdapter(), CodexAdapter())
}
