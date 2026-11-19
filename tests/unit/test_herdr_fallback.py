"""Reading a pane's agent from the process table where herdr cannot (ADR 0022)."""

from __future__ import annotations

import os
from pathlib import Path
from typing import TYPE_CHECKING, Any

import pytest

from baton_herdr.core.config import HerdrSettings
from baton_herdr.core.fakes import FakeProcessTable, FakeScreenExplainer
from baton_herdr.core.model import AgentKind, AgentState
from baton_herdr.core.panes import AgentBlockedError, AgentNotRunningError, PaneProcess
from baton_herdr.core.platform import running_on_wsl1
from baton_herdr.core.processes import ProcessEntry
from baton_herdr.herdr import reads_processes_itself
from baton_herdr.herdr.fallback import (
    ManifestTitleRules,
    ProcessFallback,
    ProcProcessTable,
    TitleRule,
    builtin_title_rules,
    herdr_state_dir,
    identify,
    parse_stat,
    title_rules_from,
    with_title,
)
from baton_herdr.herdr.host import PASTE_CHUNK_BYTES, HerdrPaneHost, paste_pieces

if TYPE_CHECKING:
    from collections.abc import Mapping, Sequence

SHELL = 41


def entry(pid: int, ppid: int, comm: str, state: str = "S") -> ProcessEntry:
    return ProcessEntry(pid=pid, ppid=ppid, comm=comm, state=state)


def rule(state: str, rule_id: str) -> dict[str, Any]:
    return {"state": state, "matched_rule": {"id": rule_id}, "fallback_reason": None}


# --- the process table ----------------------------------------------------------------


def test_descendants_are_listed_nearest_first() -> None:
    table = FakeProcessTable(
        [
            entry(1, 0, "init"),
            entry(SHELL, 1, "bash"),
            entry(60, 50, "bash"),
            entry(50, SHELL, "claude"),
            entry(45, SHELL, "sleep"),
            entry(70, 1, "codex"),
        ]
    )
    fallback = ProcessFallback(table, FakeScreenExplainer())

    assert [e.pid for e in fallback.descendants(SHELL)] == [45, 50, 60]


def test_the_agent_nearest_the_shell_is_the_panes_agent() -> None:
    table = FakeProcessTable(
        [entry(SHELL, 1, "bash"), entry(50, SHELL, "claude"), entry(60, 50, "codex")]
    )

    found = ProcessFallback(table, FakeScreenExplainer()).agent(SHELL)

    assert found is not None
    assert (found.kind, found.pid) == (AgentKind.CLAUDE, 50)


def test_a_pane_with_only_its_shell_has_no_agent() -> None:
    table = FakeProcessTable([entry(SHELL, 1, "bash"), entry(70, 1, "claude")])

    assert ProcessFallback(table, FakeScreenExplainer()).agent(SHELL) is None


@pytest.mark.parametrize(
    ("comm", "argv", "expected"),
    [
        ("claude", None, AgentKind.CLAUDE),
        ("Codex", None, AgentKind.CODEX),
        ("node", ["node", "/usr/lib/node_modules/@openai/codex/bin/codex.js"], AgentKind.CODEX),
        ("node", ["/usr/bin/node", "--no-warnings", "/opt/oc/opencode"], AgentKind.OPENCODE),
        ("python3.12", ["/usr/bin/python3.12", "/opt/fake/claude", "s.toml"], AgentKind.CLAUDE),
        ("MainThread", ["/home/u/.local/bin/claude", "--resume", "x"], AgentKind.CLAUDE),
        ("node", ["node", "server.js"], None),
        ("vim", ["vim", "claude.md"], None),
        ("bash", None, None),
    ],
)
def test_an_agent_is_known_by_its_name_or_the_script_it_runs(
    comm: str, argv: Sequence[str] | None, expected: AgentKind | None
) -> None:
    assert identify(entry(50, SHELL, comm), lambda: argv) is expected


def test_a_name_that_identifies_the_agent_spares_reading_its_cmdline() -> None:
    table = FakeProcessTable([entry(SHELL, 1, "bash"), entry(50, SHELL, "claude")])

    ProcessFallback(table, FakeScreenExplainer()).agent(SHELL)

    assert table.argv_reads == []


def test_an_exiting_process_cmdline_is_never_read() -> None:
    # On WSL that read can hang while the process exits (herdr src/platform/linux.rs).
    table = FakeProcessTable(
        [entry(SHELL, 1, "bash"), entry(50, SHELL, "node", state="Z")],
        argv={50: ["node", "codex.js"]},
    )

    assert ProcessFallback(table, FakeScreenExplainer()).agent(SHELL) is None
    assert table.argv_reads == []


def test_command_lines_fall_back_to_the_shell_itself() -> None:
    table = FakeProcessTable([entry(SHELL, 1, "bash")], argv={SHELL: ["-bash"]})

    assert ProcessFallback(table, FakeScreenExplainer()).command_lines(SHELL) == [(SHELL, "-bash")]


def test_command_lines_use_the_name_without_arguments() -> None:
    table = FakeProcessTable(
        [entry(SHELL, 1, "bash"), entry(50, SHELL, "claude")], argv={50: ["claude", "--x"]}
    )

    assert ProcessFallback(table, FakeScreenExplainer()).command_lines(SHELL) == [
        (50, "claude --x")
    ]


@pytest.mark.parametrize(
    ("stat", "expected"),
    [
        ("41 (bash) S 1 41 41 34816 0 4194560", entry(41, 1, "bash")),
        ("77 (tmux: server) R 1 77 77 0 -1", entry(77, 1, "tmux: server", state="R")),
        ("78 (a) (b)) Z 41 78 78 0 -1", entry(78, 41, "a) (b)", state="Z")),
        ("garbage", None),
        ("12 (x)", None),
    ],
)
def test_proc_stat_lines_are_parsed(stat: str, expected: ProcessEntry | None) -> None:
    assert parse_stat(stat) == expected


def test_the_proc_table_reads_stat_and_cmdline(tmp_path: Path) -> None:
    (tmp_path / "41").mkdir()
    (tmp_path / "41" / "stat").write_text("41 (bash) S 1 41 41 0 -1\n")
    (tmp_path / "41" / "cmdline").write_bytes(b"-bash\0")
    (tmp_path / "self").mkdir()
    (tmp_path / "99").mkdir()  # exited between listing and reading

    table = ProcProcessTable(tmp_path)

    assert table.entries() == [entry(41, 1, "bash")]
    assert table.argv(41) == ["-bash"]
    assert table.argv(99) is None


# --- the title rules and the platform ---------------------------------------------------


CLAUDE_RULES = builtin_title_rules(AgentKind.CLAUDE)
CODEX_RULES = builtin_title_rules(AgentKind.CODEX)


def screen(state: str, rule_id: str, priority: int) -> dict[str, Any]:
    return {
        "state": state,
        "matched_rule": {"id": rule_id, "priority": priority},
        "fallback_reason": None,
    }


PROMPT_BOX = screen("idle", "live_prompt_box", 950)
TRUST = screen("blocked", "trust_directory", 900)
FALLBACK = {
    "state": "idle",
    "matched_rule": None,
    "fallback_reason": "default_known_agent_idle_fallback",
}


@pytest.mark.parametrize(
    ("rules", "title", "screen_explain", "expected"),
    [
        # Claude's spinner outranks every screen rule.
        (CLAUDE_RULES, "\u2810 calc.py", PROMPT_BOX, "osc_title_working"),
        (CLAUDE_RULES, "\u25d0 calc.py", PROMPT_BOX, "osc_title_working"),
        # Its idle title ranks below the prompt box, above the fallback.
        (CLAUDE_RULES, "\u2733 calc.py", PROMPT_BOX, "live_prompt_box"),
        (CLAUDE_RULES, "\u2733 calc.py", FALLBACK, "osc_title_idle"),
        (CODEX_RULES, "\u2826 | sandbox", FALLBACK, "osc_title_working"),
        (CODEX_RULES, "Action Required | x", FALLBACK, "osc_title_blocked"),
        (CODEX_RULES, "user@host: ~/dev", FALLBACK, "osc_title_idle"),
        (CODEX_RULES, "user@host: ~/dev", TRUST, "trust_directory"),
        # A screen rule of the same priority stands.
        (CODEX_RULES, "\u2826 | x", screen("blocked", "b", 1050), "b"),
        (CODEX_RULES, "", FALLBACK, None),
        (CLAUDE_RULES, None, FALLBACK, None),
    ],
)
def test_title_rules_rank_against_the_screen_by_priority(
    rules: Sequence[TitleRule],
    title: str | None,
    screen_explain: dict[str, Any],
    expected: str | None,
) -> None:
    result = with_title(screen_explain, title, rules)

    assert (result.get("matched_rule") or {}).get("id") == expected


def test_a_title_rule_carries_the_state_herdr_would_report() -> None:
    result = with_title(FALLBACK, "Action Required", CODEX_RULES)

    assert result["state"] == "blocked"
    assert result["fallback_reason"] is None


@pytest.mark.parametrize(
    ("spec", "text", "expected"),
    [
        ({"contains": ["Action Required"]}, "x action required y", True),
        ({"contains": ["a", "b"]}, "a only", False),
        ({"regex": ["^\\x{2733} "]}, "\u2733 done", True),
        ({"line_regex": ["^ok$"]}, "no\nok\n", True),
        ({"any": [{"contains": ["x"]}, {"contains": ["y"]}]}, "y", True),
        ({"any": [{"contains": ["x"]}]}, "z", False),
        ({"all": [{"contains": ["x"]}, {"contains": ["y"]}]}, "x y", True),
        ({"regex": ["\\S"], "not": [{"contains": ["busy"]}]}, "busy now", False),
        ({"regex": ["\\S"], "not": [{"contains": ["busy"]}]}, "free", True),
    ],
)
def test_gates_match_as_herdr_matches_them(
    spec: dict[str, Any], text: str, *, expected: bool
) -> None:
    (rule,) = title_rules_from([{"id": "r", "state": "idle", **spec}])

    assert rule.gate.matches(text) is expected


def test_a_rule_that_does_not_compile_is_left_out() -> None:
    rules = title_rules_from(
        [{"id": "bad", "state": "idle", "regex": ["("]}, {"id": "good", "state": "idle"}, {}]
    )

    assert [rule.id for rule in rules] == ["good"]


def write_manifest(state_dir: Path, agent: str, text: str) -> Path:
    path = state_dir / "agent-detection" / "remote" / f"{agent}.toml"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return path


def test_title_rules_follow_the_manifest_herdr_fetched(tmp_path: Path) -> None:
    write_manifest(
        tmp_path,
        "codex",
        """
[[rules]]
id = "screen_rule"
state = "idle"
region = "recent"
contains = ["x"]

[[rules]]
id = "new_title_rule"
state = "working"
priority = 2000
region = "osc_title"
contains = ["busy"]
""",
    )

    rules = ManifestTitleRules(tmp_path)(AgentKind.CODEX)

    assert [(rule.id, rule.priority) for rule in rules] == [("new_title_rule", 2000)]


def test_without_a_fetched_manifest_the_built_in_rules_stand(tmp_path: Path) -> None:
    assert ManifestTitleRules(tmp_path)(AgentKind.CLAUDE) == CLAUDE_RULES


def test_a_broken_manifest_falls_back_to_the_built_in_rules(tmp_path: Path) -> None:
    write_manifest(tmp_path, "claude", "[[rules]\n")

    assert ManifestTitleRules(tmp_path)(AgentKind.CLAUDE) == CLAUDE_RULES


def test_a_changed_manifest_is_read_again(tmp_path: Path) -> None:
    rule = '[[rules]]\nid = "{}"\nstate = "idle"\nregion = "osc_title"\n'
    path = write_manifest(tmp_path, "claude", rule.format("first"))
    rules = ManifestTitleRules(tmp_path)
    assert [r.id for r in rules(AgentKind.CLAUDE)] == ["first"]

    path.write_text(rule.format("second"), encoding="utf-8")
    os.utime(path, (1, 1))

    assert [r.id for r in rules(AgentKind.CLAUDE)] == ["second"]


@pytest.mark.parametrize(
    ("env", "expected"),
    [
        ({}, Path("/home/u/.local/state/herdr")),
        ({"XDG_STATE_HOME": "/s"}, Path("/s/herdr")),
    ],
)
def test_herdrs_state_directory(env: dict[str, str], expected: Path) -> None:
    assert herdr_state_dir(env, Path("/home/u")) == expected


@pytest.mark.parametrize(
    ("release", "expected"),
    [
        ("4.4.0-26100-Microsoft\n", True),
        ("4.4.0-19041-Microsoft", True),
        ("6.6.87.2-microsoft-standard-WSL2", False),
        ("6.8.0-138-generic", False),
    ],
)
def test_wsl1_is_told_from_the_kernel_release(release: str, *, expected: bool) -> None:
    assert running_on_wsl1(release) is expected


@pytest.mark.parametrize(
    ("mode", "on_wsl1", "expected"),
    [
        ("auto", True, True),
        ("auto", False, False),
        ("baton", False, True),
        ("herdr", True, False),
    ],
)
def test_process_detection_setting(mode: str, *, on_wsl1: bool, expected: bool) -> None:
    settings = HerdrSettings.model_validate({"process_detection": mode})

    assert reads_processes_itself(settings, on_wsl1=on_wsl1) is expected


# --- the pane host ------------------------------------------------------------------------


class FakeSocket:
    """Answers herdr requests from ``answers`` and records each one."""

    def __init__(self, pane: Mapping[str, Any], shell_pid: int | None = SHELL) -> None:
        self.pane = dict(pane)
        self.shell_pid = shell_pid
        self.screen = "screen text"
        self.requests: list[tuple[str, dict[str, Any]]] = []

    async def request(self, method: str, params: Mapping[str, Any] | None = None) -> dict[str, Any]:
        self.requests.append((method, dict(params or {})))
        if method == "pane.get":
            return {"pane": self.pane}
        if method == "pane.process_info":
            info: dict[str, Any] = {"pane_id": "w1:p2"}
            if self.shell_pid is not None:
                info["shell_pid"] = self.shell_pid
            return {"process_info": info}
        if method == "pane.read":
            return {"read": {"text": self.screen}}
        if method == "pane.send_input":
            return {}
        msg = f"herdr would answer agent_not_found to {method}"
        raise AssertionError(msg)

    def methods(self) -> list[str]:
        return [method for method, _ in self.requests]


PANE = {
    "pane_id": "w1:p2",
    "agent": None,
    "agent_status": "unknown",
    "agent_session": {"source": "herdr:claude", "agent": "claude", "value": "s-1"},
    "cwd": "/w",
    "terminal_title": "",
}


def host_with(
    socket: FakeSocket, table: FakeProcessTable, explainer: FakeScreenExplainer
) -> HerdrPaneHost:
    return HerdrPaneHost(socket, fallback=ProcessFallback(table, explainer))  # type: ignore[arg-type]


def claude_table() -> FakeProcessTable:
    return FakeProcessTable([entry(SHELL, 1, "bash"), entry(50, SHELL, "claude")])


async def test_observe_reads_the_agent_from_processes_and_the_state_from_a_file() -> None:
    socket = FakeSocket(PANE)
    explainer = FakeScreenExplainer(rule("idle", "live_prompt_box"))

    observation = await host_with(socket, claude_table(), explainer).observe("w1:p2")

    assert observation.agent is AgentKind.CLAUDE
    assert (observation.state, observation.evidence) == (
        AgentState.IDLE,
        "herdr:rule:live_prompt_box",
    )
    assert observation.session_ref == "s-1"
    assert explainer.asked == [(AgentKind.CLAUDE, "screen text")]
    assert "agent.explain" not in socket.methods()


async def test_the_title_decides_before_the_screen() -> None:
    socket = FakeSocket({**PANE, "terminal_title": "⠐ calc.py"})
    explainer = FakeScreenExplainer(PROMPT_BOX)

    observation = await host_with(socket, claude_table(), explainer).observe("w1:p2")

    assert (observation.state, observation.evidence) == (
        AgentState.WORKING,
        "herdr:rule:osc_title_working",
    )
    assert explainer.asked == [(AgentKind.CLAUDE, "screen text")]


async def test_a_screen_herdr_could_not_classify_is_unknown() -> None:
    observation = await host_with(
        FakeSocket(PANE), claude_table(), FakeScreenExplainer({})
    ).observe("w1:p2")

    assert observation.agent is AgentKind.CLAUDE
    assert observation.state is AgentState.UNKNOWN


async def test_without_an_agent_process_the_pane_has_no_agent() -> None:
    explainer = FakeScreenExplainer(rule("idle", "x"))
    table = FakeProcessTable([entry(SHELL, 1, "bash")])

    observation = await host_with(FakeSocket(PANE), table, explainer).observe("w1:p2")

    assert observation.agent is None
    assert (observation.state, observation.evidence) == (AgentState.UNKNOWN, "herdr:no-agent")
    assert explainer.asked == []


async def test_without_a_shell_pid_the_pane_has_no_agent() -> None:
    observation = await host_with(
        FakeSocket(PANE, shell_pid=None), claude_table(), FakeScreenExplainer()
    ).observe("w1:p2")

    assert observation.agent is None


async def test_a_prompt_is_pasted_then_entered() -> None:
    socket = FakeSocket(PANE)
    host = host_with(socket, claude_table(), FakeScreenExplainer(rule("idle", "prompt")))

    await host.send_prompt("w1:p2", "add a power function")

    inputs = [params for method, params in socket.requests if method == "pane.send_input"]
    assert inputs == [
        {"pane_id": "w1:p2", "text": "add a power function"},
        {"pane_id": "w1:p2", "keys": ["Enter"]},
    ]


async def test_a_long_prompt_is_pasted_in_pieces_then_entered() -> None:
    socket = FakeSocket(PANE)
    host = host_with(socket, claude_table(), FakeScreenExplainer(rule("idle", "prompt")))
    prompt = "ç" * 3000  # 6000 bytes

    await host.send_prompt("w1:p2", prompt)

    inputs = [params for method, params in socket.requests if method == "pane.send_input"]
    pieces = [params["text"] for params in inputs[:-1]]
    assert "".join(pieces) == prompt
    assert all(len(piece.encode()) <= PASTE_CHUNK_BYTES for piece in pieces)
    assert len(pieces) == -(-6000 // PASTE_CHUNK_BYTES)
    assert inputs[-1] == {"pane_id": "w1:p2", "keys": ["Enter"]}


@pytest.mark.parametrize(
    "text",
    ["", "a", "a" * 2048, "a" * 2049, "aç" * 1500, "日本語" * 700, "x\ny" * 1000],
)
def test_paste_pieces_keep_the_text_and_the_limit(text: str) -> None:
    pieces = paste_pieces(text)

    assert "".join(pieces) == text
    assert all(len(piece.encode()) <= PASTE_CHUNK_BYTES for piece in pieces)
    assert all(pieces[:-1])


async def test_a_prompt_to_a_blocked_agent_is_refused() -> None:
    socket = FakeSocket(PANE)
    host = host_with(socket, claude_table(), FakeScreenExplainer(rule("blocked", "permission")))

    with pytest.raises(AgentBlockedError):
        await host.send_prompt("w1:p2", "go on")
    assert "pane.send_input" not in socket.methods()


async def test_a_prompt_without_an_agent_is_refused() -> None:
    socket = FakeSocket(PANE)
    host = host_with(socket, FakeProcessTable([entry(SHELL, 1, "bash")]), FakeScreenExplainer())

    with pytest.raises(AgentNotRunningError):
        await host.send_prompt("w1:p2", "go on")
    assert "pane.send_input" not in socket.methods()


async def test_processes_come_from_the_process_table() -> None:
    table = FakeProcessTable(
        [entry(SHELL, 1, "bash"), entry(50, SHELL, "claude")], argv={50: ["claude"]}
    )
    host = host_with(FakeSocket(PANE), table, FakeScreenExplainer())

    assert await host.processes("w1:p2") == [PaneProcess(pid=50, command="claude")]


async def test_an_idle_pane_shows_its_shell() -> None:
    table = FakeProcessTable([entry(SHELL, 1, "bash")], argv={SHELL: ["bash"]})
    host = host_with(FakeSocket(PANE), table, FakeScreenExplainer())

    assert await host.processes("w1:p2") == [PaneProcess(pid=SHELL, command="bash")]
