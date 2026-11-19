"""Stop words: "dur", "stop", "iptal" stop the work under way at once (ADR 0024).

A message made only of a stop word, a few words around it ("hemen dur", "stop it now",
"hepsini durdur") and perhaps the ids it means, is a stop. It is read here, by baton,
never by the model: it must work while the model is busy, slow, wrong or switched off.

What it stops is decided from the log (``stop_targets``): the tasks with a live attempt,
a project's step standing for its whole project. One target is cancelled at once;
"hepsini" / "all" cancels every one; otherwise the operator chooses with buttons. Any
other message, a longer sentence included, goes on to the assistant, where a cancel is a
card (ADR 0021).
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import TYPE_CHECKING, Literal

from baton_herdr.core.model import TaskId

if TYPE_CHECKING:
    from baton_herdr.core.projection import Board, TaskView

type Language = Literal["tr", "en"]

_TURKISH_STOPS = frozenset({"dur", "durdur", "durun", "iptal", "vazgeç", "bırak", "yeter", "kes"})
_ENGLISH_STOPS = frozenset({"stop", "cancel", "abort", "halt"})
_ALL = frozenset({"hepsini", "hepsi", "tümünü", "tümü", "hepsine", "all", "everything"})
# Words that may stand around a stop word without making it a sentence about something.
_FILLER = frozenset(
    {
        "et",
        "edin",
        "hemen",
        "şimdi",
        "lütfen",
        "artık",
        "bunu",
        "onu",
        "işi",
        "görevi",
        "her",
        "şey",
        "şeyi",
        "please",
        "now",
        "it",
        "that",
        "this",
        "the",
        "task",
        "work",
    }
)
# Stop words (ADR 0024): what the bot says, in the language the stop was written in.
STOP_TEXTS: dict[str, dict[str, str]] = {
    "none": {"tr": "Çalışan bir iş yok.", "en": "Nothing is running."},
    "done": {"tr": "🛑 Durduruldu:", "en": "🛑 Stopped:"},
    "which": {
        "tr": "{count} iş çalışıyor. Hangisi durdurulsun?",
        "en": "{count} pieces of work are running. Which one should stop?",
    },
    "all": {"tr": "🛑 Hepsini durdur", "en": "🛑 Stop them all"},
}

_ID = re.compile(r"^(t-[0-9a-f]{8}|p-[0-9a-f]{6})$")
_NOT_A_WORD = re.compile(r"[^\w\s-]+")


@dataclass(frozen=True, slots=True)
class StopRequest:
    language: Language
    everything: bool = False
    # The ids written with it ("dur t-1a2b3c4d"); empty: whatever is under way.
    ids: tuple[str, ...] = ()


def read_stop(message: str) -> StopRequest | None:
    """The stop a message asks for, or None when it is not one."""
    words = _words(message)
    if not words or len(words) > 6:
        return None
    turkish = any(w in _TURKISH_STOPS for w in words)
    if not turkish and not any(w in _ENGLISH_STOPS for w in words):
        return None
    allowed = _TURKISH_STOPS | _ENGLISH_STOPS | _ALL | _FILLER
    if any(w not in allowed and not _ID.match(w) for w in words):
        return None  # a sentence about something: the assistant's to read
    return StopRequest(
        language="tr" if turkish or any(not w.isascii() for w in words) else "en",
        everything=any(w in _ALL for w in words),
        ids=tuple(w for w in words if _ID.match(w)),
    )


def _words(message: str) -> list[str]:
    # Turkish casing: "İ" is "i" and "I" is "ı", which str.lower gets wrong.
    lowered = message.replace("İ", "i").replace("I", "ı").lower()
    return _NOT_A_WORD.sub(" ", lowered).split()


@dataclass(frozen=True, slots=True)
class Target:
    """What one stop cancels: a task, or the project a step of it belongs to."""

    target: str  # t-… or p-…
    title: str


def stop_targets(board: Board, request: StopRequest) -> list[Target]:
    """The work a stop is about: the ids it names, or every task with a live attempt. A
    project's step stands for its project, so the steps after it do not start."""
    targets: dict[str, Target] = {}
    if request.ids:
        for written in request.ids:
            if written.startswith("p-"):
                targets[written] = Target(written, _project_title(board, written))
            elif (task := board.tasks.get(TaskId(written))) is not None:
                targets.setdefault(*_entry(board, task))
        return list(targets.values())
    for task in board.tasks.values():
        if task.live_attempt is not None and not task.status.is_terminal:
            targets.setdefault(*_entry(board, task))
    return list(targets.values())


def _entry(board: Board, task: TaskView) -> tuple[str, Target]:
    if task.project is not None:
        return task.project, Target(task.project, _project_title(board, task.project))
    return task.task_id, Target(task.task_id, task.title)


def _project_title(board: Board, project_id: str) -> str:
    plan = next(
        (t for t in board.tasks.values() if t.project == project_id and t.role == "plan"), None
    )
    return (plan.request or plan.title) if plan is not None else project_id
