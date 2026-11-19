"""The process table and a screen classifier, for reading a pane's agent without herdr's
process detection (ADR 0022)."""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Protocol

if TYPE_CHECKING:
    from collections.abc import Mapping, Sequence

    from baton_herdr.core.model import AgentKind


@dataclass(frozen=True, slots=True)
class ProcessEntry:
    pid: int
    ppid: int
    comm: str
    # The one-letter state of /proc/<pid>/stat: R, S, D, Z, …
    state: str


class ProcessTable(Protocol):
    def entries(self) -> Sequence[ProcessEntry]:
        """Every process on the machine."""
        ...

    def argv(self, pid: int) -> Sequence[str] | None:
        """The process's arguments, or ``None`` if it has exited or hides them."""
        ...


class ScreenExplainer(Protocol):
    async def explain(self, agent: AgentKind, screen: str) -> Mapping[str, Any]:
        """herdr's ``agent.explain`` payload for ``screen`` shown by ``agent``; empty when
        the screen could not be classified."""
        ...
