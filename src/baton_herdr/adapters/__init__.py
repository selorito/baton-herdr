"""Per-agent adapters: Claude Code, Codex CLI, OpenCode (Gemini CLI in v1.1).

Agent-specific differences live only in this package, one module per agent.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from baton_herdr.adapters.claude import ClaudeAdapter
from baton_herdr.adapters.codex import CodexAdapter
from baton_herdr.adapters.opencode import OpenCodeAdapter

if TYPE_CHECKING:
    from collections.abc import Mapping

    from baton_herdr.core.agents import AgentAdapter
    from baton_herdr.core.model import AgentKind

ADAPTERS: Mapping[AgentKind, AgentAdapter] = {
    adapter.kind: adapter for adapter in (ClaudeAdapter(), CodexAdapter(), OpenCodeAdapter())
}
