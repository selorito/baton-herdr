"""Telling the operator what happened. Implementations: Telegram, logs, fakes."""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import TYPE_CHECKING, Protocol

from baton_herdr.core.logging import get_logger

if TYPE_CHECKING:
    from baton_herdr.core.model import AttemptId, OperatorAction, TaskId


class NoticeKind(StrEnum):
    TASK_STARTED = "task_started"
    TASK_HANDED_OFF = "task_handed_off"
    TASK_RESUMED = "task_resumed"
    TASK_RESTARTED = "task_restarted"
    WAITING_FOR_AGENT = "waiting_for_agent"
    NEEDS_HUMAN = "needs_human"
    TASK_COMPLETED = "task_completed"
    TASK_STOPPED = "task_stopped"


@dataclass(frozen=True, slots=True)
class Notice:
    """What happened to a task, in parts a channel can lay out.

    ``title`` is the task's name; ``text`` says what happened in one sentence;
    ``details`` add facts, one per line (the agent chosen and why, tokens spent);
    ``blocks`` are shown as they are, in a monospace font (the end of the screen,
    the files changed). Every part is plain text; channels escape it.
    """

    kind: NoticeKind
    task_id: TaskId
    text: str
    attempt_id: AttemptId | None = None
    # For a notice that asks for a decision: the blocker it is about (the seq of its
    # attempt.state_observed event) and what may be done about it remotely (ADR 0009).
    blocker_seq: int | None = None
    actions: tuple[OperatorAction, ...] = ()
    title: str = ""
    details: tuple[str, ...] = ()
    blocks: tuple[str, ...] = ()

    @property
    def plain(self) -> str:
        """Everything but the blocks, as plain lines: for logs."""
        return "\n".join(part for part in (self.title, self.text, *self.details) if part)


class Notifier(Protocol):
    async def notify(self, notice: Notice) -> None:
        """Deliver ``notice``. Must not raise: a failed delivery is not a failed task."""
        ...


class LoggingNotifier:
    """Used when no messaging channel is configured."""

    def __init__(self) -> None:
        self._log = get_logger("baton.notify")

    async def notify(self, notice: Notice) -> None:
        self._log.info("notice", kind=notice.kind.value, task_id=notice.task_id, text=notice.plain)
