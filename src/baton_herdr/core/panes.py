"""What ``core`` needs from a terminal multiplexer that hosts agents.

The vocabulary is baton's own (ADR 0004). The implementation that talks to
herdr lives in :mod:`baton_herdr.herdr`; nothing here knows herdr's protocol.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Protocol

if TYPE_CHECKING:
    from collections.abc import AsyncGenerator, Sequence

    from baton_herdr.core.model import AgentKind, AgentState


class PaneHostError(Exception):
    """The pane host refused or failed a request."""

    def __init__(self, message: str, *, code: str | None = None) -> None:
        self.code = code
        super().__init__(message)


class PaneHostUnavailableError(PaneHostError):
    """The pane host cannot be reached or did not answer in time."""


class PaneNotFoundError(PaneHostError):
    pass


class AgentNotRunningError(PaneHostError):
    """The pane exists but no agent is running in it."""


class AgentBlockedError(PaneHostError):
    """The agent is waiting for a decision; a prompt was not sent."""


@dataclass(frozen=True, slots=True)
class PaneObservation:
    pane_id: str
    agent: AgentKind | None
    state: AgentState
    # Why ``state`` was chosen, e.g. "herdr:rule:live_prompt_box" or "herdr:idle-fallback".
    evidence: str
    session_ref: str | None = None
    cwd: str | None = None


@dataclass(frozen=True, slots=True)
class PaneProcess:
    pid: int
    command: str


class PaneHost(Protocol):
    async def observe(self, pane_id: str) -> PaneObservation:
        """Current state of a pane. Raises :class:`PaneNotFoundError`."""
        ...

    def watch(self, pane_id: str) -> AsyncGenerator[PaneObservation]:
        """Yield the current observation, then a new one after every change."""
        ...

    async def read_screen(self, pane_id: str, *, lines: int = 200) -> str:
        """The last ``lines`` rows of the pane as plain text."""
        ...

    async def send_prompt(self, pane_id: str, text: str) -> None:
        """Submit ``text`` to the agent as one prompt.

        Raises :class:`AgentNotRunningError` or :class:`AgentBlockedError`.
        """
        ...

    async def send_text(self, pane_id: str, text: str) -> None:
        """Type literal text into the pane without submitting it."""
        ...

    async def send_keys(self, pane_id: str, keys: Sequence[str]) -> None:
        """Press keys such as ``Enter``, ``esc``, ``Down`` or ``ctrl+c``."""
        ...

    async def open_pane(self, *, cwd: str, label: str | None = None) -> str:
        """Open a new pane in ``cwd`` without taking focus; return its id."""
        ...

    async def close_pane(self, pane_id: str) -> None: ...

    async def find_session(self, session_ref: str) -> str | None:
        """Id of the pane that currently hosts the agent session, if any."""
        ...

    async def processes(self, pane_id: str) -> Sequence[PaneProcess]:
        """Foreground processes of the pane, for crash detection."""
        ...
