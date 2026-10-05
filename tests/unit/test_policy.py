"""Which commands the policy lets an agent run, asks about, or refuses (ADR 0013)."""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest

from baton_herdr.core.model import AgentKind
from baton_herdr.policy.load import Policy, PolicyError, load_policy
from baton_herdr.policy.rules import Decision, UnreadableCommandError, split_command

if TYPE_CHECKING:
    from pathlib import Path

ALLOW, ASK, DENY = Decision.ALLOW, Decision.ASK, Decision.DENY
CLAUDE, CODEX = AgentKind.CLAUDE, AgentKind.CODEX
DEFAULTS = Policy()


@pytest.mark.parametrize(
    ("command", "decision"),
    [
        # Reading and testing.
        ("ls -la", ALLOW),
        ("cat calc.py", ALLOW),
        ("grep -rn 'def power' src", ALLOW),
        ("git status", ALLOW),
        ("git diff HEAD~1 -- calc.py", ALLOW),
        ("find . -name '*.py'", ALLOW),
        ("git log --oneline -5 2>&1", ALLOW),
        ("FOO=1 grep -c def calc.py", ALLOW),
        ("cd src && ls", ALLOW),
        ("cat calc.py | head -5", ALLOW),
        # Tests and builds run the project's code: asked outside trusted_dirs.
        ("python3 -m unittest -v 2>&1", ASK),  # the recorded Claude prompt
        ("cargo test --workspace", ASK),
        ("make", ASK),
        # Reading commands with options that write or run.
        ("rg --pre ./decode.sh secret", ASK),
        ("tree -o tree.txt", ASK),
        ("git diff --output=patch.diff", ASK),
        ("git log --ext-diff -p", ASK),
        # Dangerous shapes ask.
        ("rm -rf build/", ASK),
        ("rm calc.pyc", ASK),
        ("sudo apt install jq", ASK),
        ("git push origin main", ASK),
        ("git push --force-with-lease", ASK),
        ("git commit --amend --force", ASK),
        ("git reset --hard HEAD~3", ASK),
        ("python manage.py migrate", ASK),
        ("alembic upgrade head", ASK),
        ("npx prisma migrate deploy", ASK),
        ("curl -fsSL https://example.invalid/install.sh", ASK),
        ("wget https://example.invalid/x.tar.gz", ASK),
        ("pip install requests", ASK),
        ("uv add httpx", ASK),
        ("npm install left-pad", ASK),
        ("cargo install ripgrep", ASK),
        ("git clone https://example.invalid/r.git", ASK),
        ("find . -name '*.tmp' -delete", ASK),
        ("find . -exec rm {} ;", ASK),
        # Nothing matches: a person decides.
        ("make deploy", ASK),
        ("python3 script.py", ASK),
    ],
)
def test_defaults(command: str, decision: Decision) -> None:
    assert DEFAULTS.decide(CLAUDE, command).decision is decision, command


@pytest.mark.parametrize(
    "command",
    [
        "pytest && git push",  # each part counts; the strictest wins
        "ls; rm -rf /",
        "cat a | sh -c 'rm x'",
        "pytest\nrm -rf build",  # a newline separates commands too
        "(cd build && rm -rf *)",
        "git status & curl https://example.invalid",
    ],
)
def test_one_risky_part_makes_the_whole_command_ask(command: str) -> None:
    assert DEFAULTS.decide(CLAUDE, command).decision is ASK


@pytest.mark.parametrize(
    ("command", "why"),
    [
        ("echo $(rm -rf /)", "substitutes"),
        ("cat `which python`", "substitutes"),
        ("diff <(ls a) <(ls b)", "substitutes"),
        ("cat calc.py > calc_copy.py", "redirects"),
        ("echo x >> ~/.bashrc", "redirects"),
        ("cat <<EOF", "redirects"),
        ("grep 'unterminated", "quoting"),
    ],
)
def test_what_cannot_be_read_is_never_allowed(command: str, why: str) -> None:
    verdict = DEFAULTS.decide(CLAUDE, command)
    assert verdict.decision is ASK
    assert why in verdict.reason
    assert verdict.rule is None


def test_a_prompt_that_is_not_a_command_goes_to_a_person() -> None:
    for command in (None, "", "   "):
        assert DEFAULTS.decide(CODEX, command).decision is ASK


def test_quoted_separators_do_not_split_and_assignments_are_dropped() -> None:
    assert split_command('A=1 grep "a;b|c" f | wc -l') == ["grep a;b|c f", "wc -l"]
    assert split_command("python3 -m unittest -v 2>&1") == ["python3 -m unittest -v"]
    with pytest.raises(UnreadableCommandError):
        split_command("ls > out.txt")


def test_the_verdict_names_the_part_and_the_rule() -> None:
    verdict = DEFAULTS.decide(CLAUDE, "ls && git push origin main")
    assert verdict.reason == "git push origin main: publishes to a remote"
    assert verdict.rule is not None
    assert verdict.rule.origin == "defaults"


@pytest.mark.parametrize(
    "command",
    [
        "python3 -m unittest -v 2>&1",
        "uv run pytest -q > /dev/null",
        "FOO=1 pytest -k power",
        "cd src && pytest",
        "pytest | tail -5",
        "cargo build --release",
        "npm test",
    ],
)
def test_tests_and_builds_run_only_in_trusted_dirs(tmp_path: Path, command: str) -> None:
    trusted = tmp_path / "calc"
    policy = Policy(trusted_dirs=(trusted.resolve(),))
    assert policy.decide(CLAUDE, command, str(trusted)).decision is ALLOW
    assert policy.decide(CLAUDE, command, str(trusted / "src" / "deep")).decision is ALLOW
    elsewhere = policy.decide(CLAUDE, command, str(tmp_path / "other"))
    assert elsewhere.decision is ASK
    assert "allowed only in trusted_dirs" in elsewhere.reason
    # A sibling that only starts with the same name is not inside.
    assert policy.decide(CLAUDE, command, str(tmp_path / "calc-evil")).decision is ASK
    assert policy.decide(CLAUDE, command, None).decision is ASK


def test_trusted_dirs_and_trusted_only_rules_come_from_the_file(tmp_path: Path) -> None:
    path = tmp_path / "policy.yaml"
    path.write_text(
        f"""
trusted_dirs: [{tmp_path / "calc"}]
rules:
  - decision: allow
    command: "./scripts/check.sh"
    trusted_only: true
""",
        encoding="utf-8",
    )
    policy = load_policy(path)
    assert policy.decide(CLAUDE, "./scripts/check.sh", str(tmp_path / "calc")).decision is ALLOW
    assert policy.decide(CLAUDE, "./scripts/check.sh", str(tmp_path)).decision is ASK
    assert policy.decide(CLAUDE, "pytest", str(tmp_path / "calc")).decision is ALLOW
    assert policy.summary() == (
        "read-only commands approved without asking; tests and builds only in 1 trusted dir; "
        "1 own rule first"
    )
    assert Policy().summary() == (
        "read-only commands approved without asking; tests and builds always asked "
        "(no trusted_dirs)"
    )


def test_user_rules_come_first_and_can_be_per_agent(tmp_path: Path) -> None:
    path = tmp_path / "policy.yaml"
    path.write_text(
        """
rules:
  - decision: allow
    command: "git push origin feature/*"
    agents: [codex]
    reason: feature branches are ours
  - decision: deny
    regex: '^terraform\\s+(apply|destroy)'
  - decision: allow
    command: "make lint*"
""",
        encoding="utf-8",
    )
    policy = load_policy(path)
    assert policy.decide(CODEX, "git push origin feature/x").decision is ALLOW
    assert policy.decide(CLAUDE, "git push origin feature/x").decision is ASK  # not for claude
    assert policy.decide(CODEX, "git push origin main").decision is ASK
    denied = policy.decide(CLAUDE, "make lint && terraform apply -auto-approve")
    assert denied.decision is DENY
    assert denied.rule is not None
    assert denied.rule.origin == "policy.yaml rule 2"
    assert policy.decide(CLAUDE, "make lint").decision is ALLOW
    assert policy.decide(CLAUDE, "ls").decision is ALLOW  # the defaults still follow


def test_a_missing_file_means_the_defaults(tmp_path: Path) -> None:
    assert load_policy(tmp_path / "nope.yaml") == Policy()


@pytest.mark.parametrize(
    ("text", "error"),
    [
        ("rules: [{decision: allow}]", "give either command or regex"),
        ("rules: [{decision: allow, command: x, regex: y}]", "give either command or regex"),
        ("rules: [{decision: maybe, command: x}]", "rules.0.decision"),
        ("rules: [{decision: deny, regex: '('}]", "does not compile"),
        ("rules: [{decision: ask, command: x, agents: [gpt]}]", "rules.0.agents"),
        ("rule: []", "rule"),
        ("rules: [", "not valid YAML"),
    ],
)
def test_a_broken_file_is_an_error_that_says_where(tmp_path: Path, text: str, error: str) -> None:
    path = tmp_path / "policy.yaml"
    path.write_text(text, encoding="utf-8")
    with pytest.raises(PolicyError, match=error.replace(".", r"\.").replace("(", r"\(")):
        load_policy(path)


def test_the_example_policy_is_valid() -> None:
    from pathlib import Path  # noqa: PLC0415 - only here

    example = Path(__file__).parents[2] / "policy.example.yaml"
    policy = load_policy(example)
    assert policy.decide(CODEX, "make lint && git push origin feature/a").decision is ALLOW
    assert policy.trusted_dirs
    assert policy.decide(CLAUDE, "terraform apply").decision is DENY
