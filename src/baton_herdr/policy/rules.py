"""Which shell commands an agent may run without asking a person (ADR 0013). Pure.

A rule says ``allow``, ``ask`` or ``deny`` for commands that match a pattern, for some or
all agents. A command is first split where the shell would run separate commands
(``;``, ``&&``, ``||``, ``|``, ``&``, newlines, parentheses); each part is decided on its
own, and the strictest decision wins: ``deny`` over ``ask`` over ``allow``. So
``pytest && git push`` asks, although ``pytest`` alone is allowed.

Within one part the first matching rule decides; nothing matching means ``ask``. Parts
are matched with leading ``NAME=value`` assignments removed and whitespace collapsed.

What cannot be read reliably is never allowed: command substitution (``$(…)``, back
quotes), process substitution, here-documents, redirection to a file, and quoting the
shell itself would not accept. Those ask. Redirections that write nowhere (``2>&1``,
``>/dev/null``) are fine.
"""

from __future__ import annotations

import fnmatch
import re
import shlex
from dataclasses import dataclass
from typing import TYPE_CHECKING

from baton_herdr.core.model import PermissionDecision as Decision

if TYPE_CHECKING:
    from collections.abc import Sequence

    from baton_herdr.core.model import AgentKind

__all__ = ["Decision", "Rule", "UnreadableCommandError", "Verdict", "decide", "split_command"]

_STRICTNESS = {Decision.ALLOW: 0, Decision.ASK: 1, Decision.DENY: 2}


@dataclass(frozen=True, slots=True)
class Rule:
    decision: Decision
    # A shell-style pattern over one command (``git push*``), or a regular expression.
    pattern: str
    reason: str
    regex: bool = False
    agents: frozenset[AgentKind] | None = None  # None: every agent
    origin: str = "defaults"  # where the rule comes from, for the event log
    # An allow that holds only in a trusted directory; elsewhere the command asks. For
    # commands that run the project's own code: tests, builds.
    trusted_only: bool = False

    def matches(self, agent: AgentKind, command: str) -> bool:
        if self.agents is not None and agent not in self.agents:
            return False
        if self.regex:
            return re.search(self.pattern, command) is not None
        return fnmatch.fnmatchcase(command, self.pattern)

    @property
    def label(self) -> str:
        """``git push* (defaults)``: the rule as a person would find it."""
        return f"{self.pattern} ({self.origin})"


@dataclass(frozen=True, slots=True)
class Verdict:
    decision: Decision
    # In words, for the event log and the operator: which part decided it, and why.
    reason: str
    # The rule behind the decision; None when no rule matched or the command was unreadable.
    rule: Rule | None = None


# Redirections that write nowhere a file could be: 2>&1, >&2, >/dev/null, 2>/dev/null, &>/dev/null.
_HARMLESS_REDIRECTS = re.compile(r"(?<![\w<>&])(?:\d?>&\d|(?:\d|&)?>>?\s*/dev/null)(?![\w/])")
_UNREADABLE = ("$(", "`", "<(", ">(")
# Tokens made only of these separate commands: ;  &&  ||  |  &  (  )  and their runs.
_SEPARATOR_CHARS = frozenset(";&|()")
_ASSIGNMENT = re.compile(r"[A-Za-z_][A-Za-z0-9_]*=")


class UnreadableCommandError(ValueError):
    """The command uses something baton does not try to understand."""


def split_command(command: str) -> list[str]:
    """The separate commands the shell would run, each as normalized words.

    Raises ``UnreadableCommandError`` for anything listed in the module docstring.
    """
    if any(marker in command for marker in _UNREADABLE):
        raise UnreadableCommandError("it substitutes the output of another command")
    text = _HARMLESS_REDIRECTS.sub(" ", command)
    lexer = shlex.shlex(text.replace("\n", " ; "), posix=True, punctuation_chars=";&|()<>")
    lexer.whitespace_split = True
    try:
        tokens = list(lexer)
    except ValueError as err:  # unbalanced quotes
        raise UnreadableCommandError("its quoting is not complete") from err
    parts: list[list[str]] = [[]]
    for token in tokens:
        if set(token) <= _SEPARATOR_CHARS:
            parts.append([])
        elif set(token) <= _SEPARATOR_CHARS | {"<", ">"}:
            raise UnreadableCommandError("it redirects input or output to a file")
        else:
            parts[-1].append(token)
    commands = []
    for words in parts:
        start = 0
        while start < len(words) and _ASSIGNMENT.match(words[start]):
            start += 1
        if words[start:]:
            commands.append(" ".join(words[start:]))
    return commands


def decide(
    rules: Sequence[Rule],
    agent: AgentKind,
    command: str | None,
    *,
    trusted: bool = False,
    workdir: str | None = None,
) -> Verdict:
    """What to do with ``agent``'s request to run ``command`` in ``workdir``.

    ``trusted`` says whether ``workdir`` is one of the policy's trusted directories, where
    ``trusted_only`` rules allow. ``None`` is a permission prompt that is not a shell
    command (a file edit, a tool, a dialog baton cannot read): a person decides.
    """
    if command is None or not command.strip():
        return Verdict(Decision.ASK, "not a shell command baton can read; a person decides")
    try:
        parts = split_command(command)
    except UnreadableCommandError as err:
        return Verdict(Decision.ASK, f"{err}; a person decides")
    if not parts:
        return Verdict(Decision.ASK, "an empty command; a person decides")
    verdicts = [_decide_one(rules, agent, part, trusted=trusted, workdir=workdir) for part in parts]
    strictest = max(verdicts, key=lambda v: _STRICTNESS[v.decision])
    if strictest.decision is not Decision.ALLOW:
        return strictest
    return Verdict(Decision.ALLOW, "; ".join(v.reason for v in verdicts), verdicts[0].rule)


def _decide_one(
    rules: Sequence[Rule], agent: AgentKind, command: str, *, trusted: bool, workdir: str | None
) -> Verdict:
    for rule in rules:
        if not rule.matches(agent, command):
            continue
        if rule.trusted_only and rule.decision is Decision.ALLOW and not trusted:
            where = workdir or "this directory"
            return Verdict(
                Decision.ASK,
                f"{_short(command)}: {rule.reason}, so it is allowed only in trusted_dirs, "
                f"and {where} is not one",
                rule,
            )
        return Verdict(rule.decision, f"{_short(command)}: {rule.reason}", rule)
    return Verdict(Decision.ASK, f"{_short(command)}: no rule matches; a person decides")


def _short(command: str, limit: int = 80) -> str:
    return command if len(command) <= limit else command[: limit - 1] + "…"
