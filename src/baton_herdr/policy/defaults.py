"""The rules every policy ends with (ADR 0013).

Only commands that read are allowed everywhere. Tests and builds run the project's own
code, which can do anything, so they are allowed only in the directories the user lists
as ``trusted_dirs``, and ask everywhere else. What deletes, publishes, rewrites history,
changes a database, raises privileges or fetches from the network asks. Everything else
asks too, because nothing matches it. A user's own rules come first and can override any
of these.
"""

from __future__ import annotations

from baton_herdr.policy.rules import Decision, Rule

ALLOW, ASK = Decision.ALLOW, Decision.ASK


def _ask(pattern: str, reason: str, *, regex: bool = False) -> Rule:
    return Rule(ASK, pattern, reason, regex=regex)


def _allow(patterns: str, reason: str, *, trusted_only: bool = False) -> list[Rule]:
    return [
        Rule(ALLOW, pattern, reason, trusted_only=trusted_only) for pattern in patterns.split("|")
    ]


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
    # Reading commands with options that write a file or run a program.
    _ask(r"^rg\s(.*\s)?--pre(=|\s|$)", "runs a program on each file", regex=True),
    _ask(r"^tree\s(.*\s)?-o(\s|$)", "writes a file", regex=True),
    _ask(
        r"^git\s.*\s--(output|ext-diff)(=|\s|$)",
        "writes a file or runs a diff program",
        regex=True,
    ),
    # Reading.
    *_allow(
        "ls|ls *|cat *|head *|tail *|wc *|grep *|rg *|find *|tree|tree *|file *|stat *|du *"
        "|pwd|cd *|which *|echo *|diff *|git status*|git diff*|git log*|git show*"
        "|git blame *|git rev-parse *|git ls-files*|git branch|git branch --list*",
        "reads only",
    ),
    # Tests and builds run the project's code: only in trusted_dirs.
    *_allow(
        "pytest|pytest *|python -m pytest*|python3 -m pytest*|python -m unittest*"
        "|python3 -m unittest*|uv run pytest*|uv run python -m pytest*|cargo test*"
        "|cargo nextest *|cargo build*|cargo check*|cargo clippy*|go test*|go build*|go vet*"
        "|npm test*|npm run test*|npm run build*|pnpm test*|yarn test*|tsc|tsc *"
        "|just test*|just check*|make|make test*|make check*|make build*"
        "|mvn test*|gradle test*|./gradlew test*",
        "runs the project's own code (tests, builds)",
        trusted_only=True,
    ),
)
