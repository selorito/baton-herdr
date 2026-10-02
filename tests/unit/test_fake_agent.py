from __future__ import annotations

import io
import re
from datetime import UTC, datetime
from pathlib import Path

import pytest

from coban.adapters import ADAPTERS
from coban.core.detection import DetectionRequest
from coban.core.model import AgentKind, AgentState

import fake_agent

SCRIPTS = Path(__file__).parents[2] / "tools" / "fake-agent" / "scripts"
NOW = datetime(2026, 10, 1, 14, 15, tzinfo=UTC)


class RecordingReporter:
    def __init__(self) -> None:
        self.sessions: list[str] = []

    def session(self, session_id: str) -> None:
        self.sessions.append(session_id)


def run(script_name: str, stdin: str) -> tuple[RecordingReporter, list[str], list[float]]:
    reporter = RecordingReporter()
    out = io.StringIO()
    slept: list[float] = []
    fake_agent.play(
        fake_agent.load_script(SCRIPTS / script_name),
        session_id="sess-1",
        reporter=reporter,
        terminal=fake_agent.Terminal(
            stdin=io.StringIO(stdin), stdout=out, now=lambda: NOW, sleep=slept.append
        ),
    )
    return reporter, out.getvalue().split(fake_agent.CLEAR)[1:], slept


def test_claude_limit_script_reports_and_waits_like_an_agent() -> None:
    reporter, screens, slept = run("claude-limit.toml", "add a power function\n/exit\n")

    # Only the session is reported; herdr reads the state from the screens.
    assert reporter.sessions == ["sess-1"]
    assert len(screens) == 3
    assert "❯ add a power function" in screens[1]
    assert "resets 2:45pm" in screens[2]
    assert slept == [2.0]


def test_the_script_stops_when_stdin_closes() -> None:
    _, screens, _ = run("codex-finish.toml", "")
    assert len(screens) == 1


@pytest.mark.parametrize(
    ("script_name", "host_states", "expected"),
    [
        # Host states as herdr derives them from these screens with its own rules.
        (
            "claude-limit.toml",
            [AgentState.IDLE, AgentState.WORKING, AgentState.IDLE],
            [AgentState.IDLE, AgentState.WORKING, AgentState.RATE_LIMITED],
        ),
        (
            "codex-finish.toml",
            # Even without herdr's working signal, coban sees the work on the screen.
            [AgentState.UNKNOWN, AgentState.UNKNOWN, AgentState.UNKNOWN],
            [AgentState.IDLE, AgentState.WORKING, AgentState.IDLE],
        ),
    ],
)
def test_coban_reads_the_fake_screens_as_intended(
    script_name: str, host_states: list[AgentState], expected: list[AgentState]
) -> None:
    script = fake_agent.load_script(SCRIPTS / script_name)
    agent = AgentKind(script.agent)
    _, screens, _ = run(script_name, "task\n")
    states = [
        ADAPTERS[agent]
        .classify(
            DetectionRequest(
                agent=agent, screen=screen, host_state=host, observed_at=NOW, timezone="UTC"
            )
        )
        .state
        for screen, host in zip(screens, host_states, strict=True)
    ]
    assert states == expected


def test_limit_screen_reset_time_is_read_back_by_the_adapter() -> None:
    _, screens, _ = run("claude-limit.toml", "task\n")
    request = DetectionRequest(
        agent=AgentKind.CLAUDE,
        screen=screens[2],
        host_state=AgentState.IDLE,
        observed_at=NOW,
        timezone="UTC",
    )
    assert ADAPTERS[AgentKind.CLAUDE].classify(request).resets_at == datetime(
        2026, 10, 1, 14, 45, tzinfo=UTC
    )


@pytest.mark.parametrize(
    ("text", "message"),
    [
        ("[[step]]\nwait = 1\n", "'agent' must be"),
        ('agent = "claude"\n', "no [[step]]"),
        ('agent = "claude"\n[[step]]\nwait = "later"\n', "wait must be"),
    ],
)
def test_invalid_scripts_are_rejected(tmp_path: Path, text: str, message: str) -> None:
    path = tmp_path / "bad.toml"
    path.write_text(text)
    with pytest.raises(fake_agent.ScriptError, match=re.escape(message)):
        fake_agent.load_script(path)


def test_reporting_targets_herdr_only_inside_a_pane() -> None:
    inside = fake_agent.reporter_for("codex", {"HERDR_PANE_ID": "w1:p3", "HERDR_BIN_PATH": "/h"})
    assert isinstance(inside, fake_agent.HerdrReporter)
    assert (inside.pane_id, inside.agent, inside.binary) == ("w1:p3", "codex", "/h")
    assert isinstance(fake_agent.reporter_for("codex", {}), fake_agent.SilentReporter)


def test_launcher_runs_the_fake_under_the_agent_name(tmp_path: Path) -> None:
    launcher = fake_agent.make_launcher(tmp_path / "bin", "claude")

    assert launcher.name == "claude"
    assert launcher.stat().st_mode & 0o111
    first_line, *rest = launcher.read_text().splitlines()
    assert first_line.startswith("#!/")  # absolute interpreter, not /usr/bin/env
    assert "env" not in first_line.split("/")[-1]
    assert any("fake_agent.py" in line for line in rest)


def test_codex_script_sets_a_spinner_title_while_working() -> None:
    reporter = RecordingReporter()
    out = io.StringIO()
    fake_agent.play(
        fake_agent.load_script(SCRIPTS / "codex-finish.toml"),
        session_id="s",
        reporter=reporter,
        terminal=fake_agent.Terminal(
            stdin=io.StringIO("task\n"), stdout=out, now=lambda: NOW, sleep=lambda _s: None
        ),
    )
    titles = re.findall(r"\x1b\]0;([^\x07]*)\x07", out.getvalue())
    assert titles == ["coban-sandbox", "⠏ | coban-sandbox", "coban-sandbox"]
