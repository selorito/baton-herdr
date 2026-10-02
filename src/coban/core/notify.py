"""Telling the operator what happened. Implementations: Telegram, logs, fakes."""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import TYPE_CHECKING, Protocol

from coban.core.logging import get_logger

if TYPE_CHECKING:
    from coban.core.model import AttemptId, TaskId


class NoticeKind(StrEnum):
    TASK_STARTED = "task_started"
    AGENT_LIMITED = "agent_limited"
    TASK_HANDED_OFF = "task_handed_off"
    TASK_RESUMED = "task_resumed"
    TASK_RESTARTED = "task_restarted"
    WAITING_FOR_AGENT = "waiting_for_agent"
    NEEDS_HUMAN = "needs_human"
    TASK_COMPLETED = "task_completed"
    TASK_STOPPED = "task_stopped"


@dataclass(frozen=True, slots=True)
class Notice:
    kind: NoticeKind
    task_id: TaskId
    text: str
    attempt_id: AttemptId | None = None


class Notifier(Protocol):
    async def notify(self, notice: Notice) -> None:
        """Deliver ``notice``. Must not raise: a failed delivery is not a failed task."""
        ...


class LoggingNotifier:
    """Used when no messaging channel is configured."""

    def __init__(self) -> None:
        self._log = get_logger("coban.notify")

    async def notify(self, notice: Notice) -> None:
        self._log.info("notice", kind=notice.kind.value, task_id=notice.task_id, text=notice.text)
