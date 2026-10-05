"""The rules every policy ends with (ADR 0013).

Reading and running tests are allowed. What deletes, publishes, rewrites history,
changes a database, raises privileges or fetches from the network asks. Everything else
asks too, because nothing matches it. A user's own rules come first and can override
any of these.

Allowing test runs is a trade: a test can run any code the repository contains. It is
allowed because an agent that may not run the tests cannot finish most tasks alone.
"""

from __future__ import annotations

from baton_herdr.policy.rules import Decision, Rule

ALLOW, ASK = Decision.ALLOW, Decision.ASK


def _ask(pattern: str, reason: str, *, regex: bool = False) -> Rule:
    return Rule(ASK, pattern, reason, regex=regex)


def _allow(patterns: str, reason: str) -> list[Rule]:
    return [Rule(ALLOW, pattern, reason) for pattern in patterns.split("|")]


DEFAULT_RULES: tuple[Rule, ...] = (
    # First the dangerous shapes, so that no allow rule below can let one through.
    _ask(r"^(sudo|doas|su|pkexec)(\s|$)", "runs with raised privileges", regex=True),
    _ask(r"^rm(\s|$)", "deletes files", regex=True),
    _ask(
        r"(^|\s)(--force(-with-lease)?|--force-push)(\s|=|$)",
        "forces past a safety check",
        regex=True,
    ),
    _ask(r"^git\s+push(\s|$)", "publishes to a remote", regex=True),
    _ask(
        r"^git\s+(reset\s+--hard|clean|checkout\s+--|restore|rebase|filter-branch|branch\s+-D)",
        "can throw away work",
        regex=True,
    ),
    _ask(
        r"(^|\s)(migrate|migration|migrations|db:migrate|db:rollback)(\s|:|$)"
        r"|^alembic\s+(upgrade|downgrade|stamp)",
        "changes a database schema",
        regex=True,
    ),
    _ask(
        r"^(curl|wget|scp|rsync|ssh|nc|ftp|aria2c)(\s|$)"
        r"|^git\s+(clone|fetch|pull)(\s|$)"
        r"|^(pip|pip3|pipx|uv\s+pip|npm|pnpm|yarn|bun|cargo|go|gem|brew|apt|apt-get|dnf)"
        r"\s+(install|add|i|get|update|upgrade)(\s|$)"
        r"|^uv\s+(add|sync|tool\s+install)(\s|$)",
        "downloads from the network",
        regex=True,
    ),
    _ask(
        r"^find\s.*\s-(delete|exec|execdir|ok|okdir|fprint\S*)(\s|$)",
        "find that acts on files",
        regex=True,
    ),
    # Reading.
    *_allow(
        "ls|ls *|cat *|head *|tail *|wc *|grep *|rg *|find *|tree|tree *|file *|stat *|du *"
        "|pwd|cd *|which *|echo *|diff *|git status*|git diff*|git log*|git show*"
        "|git blame *|git rev-parse *|git ls-files*|git branch|git branch --list*",
        "reads only",
    ),
    # Running tests.
    *_allow(
        "pytest|pytest *|python -m pytest*|python3 -m pytest*|python -m unittest*"
        "|python3 -m unittest*|uv run pytest*|uv run python -m pytest*|cargo test*"
        "|cargo nextest *|go test*|npm test*|npm run test*|pnpm test*|yarn test*"
        "|just test*|just check*|make test*|make check*|mvn test*|gradle test*|./gradlew test*",
        "runs the tests",
    ),
)
