"""Which task an action in words is for (ADR 0021). Pure.

The model passes on how the operator named the task: an id, or words ("the cafe task").
baton alone decides which task that is, from the log. An id the operator did not write is
not taken from the model; words that fit no open task, or more than one, get a question,
never a guess.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import TYPE_CHECKING

from baton_herdr.core.model import TaskId

if TYPE_CHECKING:
    from collections.abc import Sequence

    from baton_herdr.core.assistant import TaskRef
    from baton_herdr.core.projection import Board, TaskView

# Words that say "a task" rather than which one.
_FILLER = frozenset(
    {
        "the",
        "a",
        "an",
        "task",
        "tasks",
        "job",
        "project",
        "one",
        "iş",
        "işi",
        "işini",
        "görev",
        "görevi",
        "proje",
        "projesi",
        "projeyi",
    }
)
# More than this many candidates: the question lists the first ones only.
MAX_CHOICES = 5


@dataclass(frozen=True, slots=True)
class OneTask:
    task: TaskView


@dataclass(frozen=True, slots=True)
class OneProject:
    project_id: str
    # Its tasks still open, which cancelling it drops.
    open_tasks: tuple[TaskView, ...]


@dataclass(frozen=True, slots=True)
class Closed:
    """The task or project named is in the log and already closed: nothing to do."""

    named: str
    # How it closed: "completed", "cancelled"; "closed" for a project.
    status: str


@dataclass(frozen=True, slots=True)
class Ambiguous:
    candidates: tuple[TaskView, ...]


@dataclass(frozen=True, slots=True)
class Unknown:
    # The id or the words that named nothing; empty: nothing was named.
    named: str = ""


type Found = OneTask | OneProject | Closed | Ambiguous | Unknown


def find_target(board: Board, ref: TaskRef, *, written: Sequence[str], projects: bool) -> Found:
    """The task ``ref`` names on ``board``. An id counts only when it is in one of the
    ``written`` texts (the operator's message, the conversation): one the model wrote by
    itself is set aside for the words, if any. ``projects``: a project id is a target of
    its own (cancel); otherwise it is asked which of its open tasks is meant (tier)."""
    if ref.id is not None and any(ref.id in text for text in written):
        return _by_id(board, ref.id, projects=projects)
    if ref.words is None:
        return Unknown()
    return _by_words(board, ref.words)


def _by_id(board: Board, wanted: str, *, projects: bool) -> Found:
    if wanted.startswith("p-"):
        tasks = [t for t in board.tasks.values() if t.project == wanted]
        if not tasks:
            return Unknown(wanted)
        still_open = tuple(t for t in tasks if not t.status.is_terminal)
        if projects:
            return OneProject(wanted, still_open) if still_open else Closed(wanted, "closed")
        if len(still_open) == 1:
            return OneTask(still_open[0])
        return Ambiguous(still_open) if still_open else Closed(wanted, "closed")
    task = board.tasks.get(TaskId(wanted))
    if task is None:
        return Unknown(wanted)
    return Closed(wanted, task.status.value) if task.status.is_terminal else OneTask(task)


def _by_words(board: Board, words: str) -> Found:
    """Open tasks whose title or request holds every word that names one."""
    wanted = [w for w in re.findall(r"\w+", words.lower()) if w not in _FILLER]
    if not wanted:
        return Unknown(words)
    found = tuple(
        t
        for t in board.tasks.values()
        if not t.status.is_terminal
        and all(w in f"{t.title} {t.request or ''}".lower() for w in wanted)
    )
    if not found:
        return Unknown(words)
    return OneTask(found[0]) if len(found) == 1 else Ambiguous(found)
