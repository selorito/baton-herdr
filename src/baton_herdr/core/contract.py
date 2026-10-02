"""The end-of-turn contract between baton and an agent.

Every prompt baton sends asks the agent to end its final message with one line,

    [[BATON:END status=done]]       the task is finished
    [[BATON:END status=question]]   the agent needs an answer, asked just above the line
    [[BATON:END status=blocked]]    the agent cannot go on, and says why just above it

so that a turn that ended with a question in plain text is not mistaken for a
finished task. The mark is an extra signal on top of screen detection: a missing
or malformed mark means "no signal", never an error, and it never overrides a
positive detection such as a permission dialog or a usage limit (see the design
note in docs/research/agents.md).
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from enum import StrEnum

# Spelled with a placeholder, so the prompt echoed on screen never reads as a mark.
# One line, like every prompt baton sends (see ``one_line``).
END_CONTRACT = (
    "When you stop, end your final message with a line of its own reading "
    "[[BATON:END status=<done|question|blocked>]] with one status filled in: done if the "
    "task is finished; question if you need an answer from me, asked just above that line; "
    "blocked if you cannot continue, with the reason just above that line."
)

# Evidence for a turn that ended waiting for the operator's words: a question asked
# with the mark, or a turn cut short by a denied permission ("what should I do
# instead?"). Only these may be answered with free text (ADR 0009).
QUESTION_EVIDENCE = "baton:end:question"
DENIED_EVIDENCE = "baton:denied"
ANSWERABLE_EVIDENCE = frozenset({QUESTION_EVIDENCE, DENIED_EVIDENCE})

_LINE_BREAKS = re.compile(r"[ \t]*(?:\r?\n)+[ \t]*")
_MARK = re.compile(r"\[\[BATON:END status=(done|question|blocked)\]\]")
# Lines above a mark that are quoted as the agent's question or reason.
_CONTEXT_LINES = 6
_CONTEXT_CHARS = 500
# Box drawing, bullets and other TUI decoration around an agent's message.
_DECORATION = " \t│┃|>⏺●•◆▌▎─━╭╮╰╯"


def one_line(text: str) -> str:
    """``text`` with its line breaks turned into spaces.

    Agents receive prompts through the terminal. A prompt with line breaks
    arrives as a paste, and Claude Code (2026-10) declines to act on a message
    that is only pasted text; a single line arrives as typed input.
    """
    return _LINE_BREAKS.sub(" ", text).strip()


class EndStatus(StrEnum):
    DONE = "done"
    QUESTION = "question"
    BLOCKED = "blocked"


@dataclass(frozen=True, slots=True)
class EndMark:
    status: EndStatus
    # What the agent wrote just above the mark: the question or the reason.
    text: str


def parse_end(screen: str) -> EndMark | None:
    """The last end mark on ``screen``, if any; earlier marks are older turns."""
    lines = screen.splitlines()
    for index in range(len(lines) - 1, -1, -1):
        found = list(_MARK.finditer(lines[index]))
        if found:
            status = EndStatus(found[-1].group(1))
            return EndMark(status, _context(lines[:index]))
    return None


def _context(lines: list[str]) -> str:
    kept: list[str] = []
    for line in reversed(lines):
        text = line.strip(_DECORATION)
        if not text:
            if kept:
                break  # the paragraph right above the mark
            continue
        kept.append(text)
        if len(kept) == _CONTEXT_LINES:
            break
    joined = " ".join(reversed(kept))
    return joined if len(joined) <= _CONTEXT_CHARS else joined[: _CONTEXT_CHARS - 1] + "…"
