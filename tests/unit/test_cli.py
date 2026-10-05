from __future__ import annotations

import json

from typer.testing import CliRunner

from baton_herdr.cli import app

runner = CliRunner()


def request(agent: str, screen: str, host: str = "unknown") -> str:
    return json.dumps(
        {
            "agent": agent,
            "screen": screen,
            "host_state": host,
            "observed_at": "2026-10-01T11:00:00Z",
            "timezone": "Europe/Istanbul",
        }
    )


def test_version_prints_the_package_version() -> None:
    result = runner.invoke(app, ["version"])
    assert result.exit_code == 0
    assert result.stdout.startswith("baton ")


def test_detect_answers_each_ndjson_line_in_order() -> None:
    lines = [
        request("claude", "You've hit your session limit · resets 3:45pm\n❯\n", host="idle"),
        "",
        request("gemini", "anything\n", host="working"),  # no adapter in v1
    ]
    result = runner.invoke(app, ["detect"], input="\n".join(lines) + "\n")

    assert result.exit_code == 0
    answers = [json.loads(line) for line in result.stdout.splitlines()]
    assert answers == [
        {
            "contract": 1,
            "state": "rate_limited",
            "evidence": "baton:claude_usage_limit",
            "resets_at": "2026-10-01T12:45:00Z",
        },
        {"contract": 1, "state": "working", "evidence": "baton:no-adapter", "resets_at": None},
    ]


def test_detect_rejects_an_invalid_line_with_its_number() -> None:
    result = runner.invoke(app, ["detect"], input=request("claude", "x") + "\n{not json}\n")

    assert result.exit_code == 2
    assert "line 2: invalid detection request" in result.stderr


def test_policy_check_says_what_would_happen_and_why() -> None:
    allowed = runner.invoke(app, ["policy", "check", "ls && git status"])
    assert allowed.exit_code == 0
    assert allowed.stdout.splitlines()[0] == "allow: ls: reads only; git status: reads only"
    tests = runner.invoke(app, ["policy", "check", "pytest -q", "--dir", "/srv/calc"])
    assert tests.stdout.splitlines()[0] == (
        "ask: pytest -q: runs the project's own code (tests, builds), so it is allowed only "
        "in trusted_dirs, and /srv/calc is not one"
    )
    asked = runner.invoke(app, ["policy", "check", "git push --force", "--agent", "codex"])
    assert asked.stdout.splitlines()[:2] == [
        "ask: git push --force: forces past a safety check",
        "rule: (^|\\s)(--force(-with-lease)?|--force-push)(\\s|=|$) (defaults)",
    ]
    assert runner.invoke(app, ["policy", "check", "ls", "--agent", "gpt"]).exit_code == 2
