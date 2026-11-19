from __future__ import annotations

import json
from pathlib import Path
from typing import TYPE_CHECKING

from typer.testing import CliRunner

from baton_herdr import cli
from baton_herdr.cli import app

from detector_binary import detector
from platform_marks import linux_only

if TYPE_CHECKING:
    import pytest

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
    env = {"BATON_DETECTOR__BINARY": str(detector())}
    result = runner.invoke(app, ["detect"], input="\n".join(lines) + "\n", env=env)

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
    env = {"BATON_DETECTOR__BINARY": str(detector())}
    result = runner.invoke(
        app, ["detect"], input=request("claude", "x") + "\n{not json}\n", env=env
    )

    assert result.exit_code == 2
    assert "line 2: invalid detection request" in result.stderr


def test_policy_check_says_what_would_happen_and_why() -> None:
    allowed = runner.invoke(app, ["policy", "check", "ls && git status"])
    assert allowed.exit_code == 0
    assert allowed.stdout.splitlines()[0] == "allow: ls: reads only; git status: reads only"
    tests = runner.invoke(app, ["policy", "check", "pytest -q", "--dir", "/srv/calc"])
    assert tests.stdout.splitlines()[0] == (
        "ask: pytest -q: runs the project's own code (tests, builds), so it is allowed only "
        f"in trusted_dirs, and {Path('/srv/calc').resolve()} is not one"
    )
    asked = runner.invoke(app, ["policy", "check", "git push --force", "--agent", "codex"])
    assert asked.stdout.splitlines()[:2] == [
        "ask: git push --force: forces past a safety check",
        "rule: (^|\\s)(--force(-with-lease)?|--force-push)(\\s|=|$) (defaults)",
    ]
    assert runner.invoke(app, ["policy", "check", "ls", "--agent", "gpt"]).exit_code == 2


@linux_only("WSL1 is Linux; status reads /proc")
def test_service_install_on_wsl1_writes_the_start_script_and_prints_the_task(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    (bin_dir / "herdr").write_text("#!/bin/sh\n")
    (bin_dir / "herdr").chmod(0o755)
    monkeypatch.setattr(cli, "running_on_wsl1", lambda: True)
    monkeypatch.setattr(cli, "service_kind", lambda: "wsl1")
    env = {
        "HOME": str(tmp_path),
        "XDG_CONFIG_HOME": str(tmp_path / "config"),
        "PATH": f"{bin_dir}:/usr/bin:/bin",
        "BATON_CONFIG": str(tmp_path / "none.toml"),
        "BATON_USAGE__ENABLED": "false",
        "WSL_DISTRO_NAME": "Ubuntu-24.04",
    }
    script = tmp_path / ".local" / "bin" / "baton-wsl1-start"

    result = runner.invoke(app, ["service", "install"], env=env)
    assert result.exit_code == 0, result.output
    assert f"wrote {script}" in result.stdout
    assert f"-Argument '-d Ubuntu-24.04 -- {script}'" in result.stdout
    assert "systemctl" not in result.stdout
    assert not (tmp_path / "config" / "systemd").exists()
    assert "--session baton server" in script.read_text()

    status = runner.invoke(app, ["service", "status"], env=env)
    assert status.stdout.startswith(f"start script: {script} (herdr session 'baton')")

    removed = runner.invoke(app, ["service", "uninstall"], env=env)
    assert f"removed {script}" in removed.stdout
    assert "Unregister-ScheduledTask" in removed.stdout
    assert not script.exists()


def test_setup_without_a_terminal_says_so_instead_of_waiting_on_a_question() -> None:
    # CliRunner's stdin is not a terminal, as an agent's or a CI's is not.
    result = runner.invoke(app, ["setup"])
    assert result.exit_code == 2
    assert "needs a terminal" in result.stderr
