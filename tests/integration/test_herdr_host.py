"""HerdrPaneHost against a stand-in herdr server speaking the real wire format."""

from __future__ import annotations

import asyncio
import json
from typing import TYPE_CHECKING, Any

import pytest

from coban.core.model import AgentKind, AgentState
from coban.core.panes import (
    AgentBlockedError,
    AgentNotRunningError,
    PaneHostUnavailableError,
    PaneNotFoundError,
    PaneProcess,
)
from coban.herdr.host import HerdrPaneHost
from coban.herdr.transport import HerdrSocket

if TYPE_CHECKING:
    from collections.abc import AsyncIterator
    from pathlib import Path

PANE = {
    "pane_id": "w1:p6",
    "agent": "claude",
    "agent_status": "idle",
    "cwd": "/home/u/dev/coban-sandbox",
    "foreground_cwd": "/home/u/dev/coban-sandbox",
    "agent_session": {"agent": "claude", "kind": "id", "source": "herdr:claude", "value": "sess-1"},
}
EXPLAIN = {"agent": "claude", "state": "idle", "matched_rule": {"id": "live_prompt_box"}}
WORKSPACES = [
    {"workspace_id": "w1", "label": "my project"},
    {"workspace_id": "w2", "label": "coban"},
]


class StandInHerdr:
    """Answers requests from ``replies`` and pushes ``events`` to subscribers."""

    def __init__(self) -> None:
        self.replies: dict[str, Any] = {}
        self.requests: list[dict[str, Any]] = []
        self.events: asyncio.Queue[dict[str, Any]] = asyncio.Queue()
        self.subscriptions = 0
        self.handlers: set[asyncio.Task[None]] = set()

    def ok(self, method: str, result: dict[str, Any]) -> None:
        self.replies[method] = {"result": result}

    def fail(self, method: str, code: str) -> None:
        self.replies[method] = {"error": {"code": code, "message": f"{code} happened"}}

    async def handle(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        task = asyncio.current_task()
        assert task is not None
        self.handlers.add(task)
        request = json.loads(await reader.readline())
        self.requests.append(request)

        def send(body: dict[str, Any]) -> None:
            writer.write(json.dumps({"id": request["id"], **body}).encode() + b"\n")

        if request["method"] == "events.subscribe":
            self.subscriptions += 1
            send({"result": {"type": "subscription_started"}})
            await writer.drain()
            while True:
                pushed = await self.events.get()
                if "error" in pushed:
                    send(pushed)
                else:
                    writer.write(json.dumps(pushed).encode() + b"\n")
                await writer.drain()
                if "error" in pushed:
                    break
        else:
            send(self.replies[request["method"]])
            await writer.drain()
        writer.close()


@pytest.fixture
async def herdr(tmp_path: Path) -> AsyncIterator[tuple[StandInHerdr, HerdrPaneHost]]:
    stand_in = StandInHerdr()
    socket_path = tmp_path / "herdr.sock"
    server = await asyncio.start_unix_server(stand_in.handle, path=str(socket_path))
    async with server:
        yield stand_in, HerdrPaneHost(HerdrSocket(socket_path, timeout_s=2))
        # Subscription handlers wait for events forever; stop them so the server can close.
        for handler in stand_in.handlers:
            handler.cancel()


async def test_observe_combines_pane_info_and_explain(
    herdr: tuple[StandInHerdr, HerdrPaneHost],
) -> None:
    stand_in, host = herdr
    stand_in.ok("pane.get", {"type": "pane_info", "pane": PANE})
    stand_in.ok("agent.explain", {"type": "agent_explain", "explain": EXPLAIN})

    observation = await host.observe("w1:p6")

    assert observation.agent is AgentKind.CLAUDE
    assert (observation.state, observation.evidence) == (
        AgentState.IDLE,
        "herdr:rule:live_prompt_box",
    )
    assert (observation.session_ref, observation.cwd) == ("sess-1", "/home/u/dev/coban-sandbox")
    assert [r["method"] for r in stand_in.requests] == ["pane.get", "agent.explain"]
    assert stand_in.requests[1]["params"] == {"target": "w1:p6"}


async def test_a_pane_without_an_agent_is_unknown_and_explain_is_not_called(
    herdr: tuple[StandInHerdr, HerdrPaneHost],
) -> None:
    stand_in, host = herdr
    shell = {"pane_id": "w1:p2", "agent_status": "unknown", "cwd": "/srv"}
    stand_in.ok("pane.get", {"type": "pane_info", "pane": shell})

    observation = await host.observe("w1:p2")

    assert (observation.agent, observation.state) == (None, AgentState.UNKNOWN)
    assert observation.evidence == "herdr:no-agent"
    assert len(stand_in.requests) == 1


async def test_an_agent_that_exits_between_the_two_reads_is_no_agent(
    herdr: tuple[StandInHerdr, HerdrPaneHost],
) -> None:
    stand_in, host = herdr
    stand_in.ok("pane.get", {"type": "pane_info", "pane": PANE})
    stand_in.fail("agent.explain", "agent_not_found")

    observation = await host.observe("w1:p6")

    assert (observation.agent, observation.state) == (None, AgentState.UNKNOWN)


@pytest.mark.parametrize(
    ("code", "error"),
    [
        ("pane_not_found", PaneNotFoundError),
        ("agent_not_found", AgentNotRunningError),
        ("agent_blocked", AgentBlockedError),
    ],
)
async def test_herdr_errors_become_pane_host_errors(
    herdr: tuple[StandInHerdr, HerdrPaneHost], code: str, error: type[Exception]
) -> None:
    stand_in, host = herdr
    stand_in.fail("agent.prompt", code)

    with pytest.raises(error, match=f"{code} happened"):
        await host.send_prompt("w1:p6", "hello")


async def test_commands_use_herdrs_request_shapes(
    herdr: tuple[StandInHerdr, HerdrPaneHost],
) -> None:
    stand_in, host = herdr
    for method in ("agent.prompt", "pane.send_text", "pane.send_keys", "pane.close"):
        stand_in.ok(method, {"type": "ok"})
    stand_in.ok("workspace.list", {"type": "workspace_list", "workspaces": WORKSPACES})
    stand_in.ok("tab.create", {"type": "tab_created", "tab": {}, "root_pane": {"pane_id": "w1:p9"}})
    stand_in.ok("pane.read", {"type": "pane_read", "read": {"text": "a\nb\n"}})
    processes = [{"pid": 42, "cmdline": "claude --resume x"}]
    stand_in.ok(
        "pane.process_info",
        {"type": "pane_process_info", "process_info": {"foreground_processes": processes}},
    )

    await host.send_prompt("w1:p6", "do it")
    await host.send_text("w1:p6", "/exit")
    await host.send_keys("w1:p6", ["Enter"])
    assert await host.open_pane(cwd="/work", label="task 1") == "w1:p9"
    assert await host.read_screen("w1:p6", lines=50) == "a\nb\n"
    assert await host.processes("w1:p6") == [PaneProcess(pid=42, command="claude --resume x")]
    await host.close_pane("w1:p9")

    assert [(r["method"], r["params"]) for r in stand_in.requests] == [
        ("agent.prompt", {"target": "w1:p6", "text": "do it"}),
        ("pane.send_text", {"pane_id": "w1:p6", "text": "/exit"}),
        ("pane.send_keys", {"pane_id": "w1:p6", "keys": ["Enter"]}),
        ("workspace.list", {}),
        (
            "tab.create",
            {"workspace_id": "w2", "cwd": "/work", "label": "task 1", "focus": False},
        ),
        ("pane.read", {"pane_id": "w1:p6", "source": "recent", "lines": 50, "strip_ansi": True}),
        ("pane.process_info", {"pane_id": "w1:p6"}),
        ("pane.close", {"pane_id": "w1:p9"}),
    ]


async def test_the_first_pane_creates_cobans_own_workspace(
    herdr: tuple[StandInHerdr, HerdrPaneHost],
) -> None:
    stand_in, host = herdr
    stand_in.ok("workspace.list", {"type": "workspace_list", "workspaces": WORKSPACES[:1]})
    created = {
        "type": "workspace_created",
        "workspace": {},
        "tab": {},
        "root_pane": {"pane_id": "w3:p1"},
    }
    stand_in.ok("workspace.create", created)

    assert await host.open_pane(cwd="/work", label="task 1") == "w3:p1"
    assert [(r["method"], r["params"]) for r in stand_in.requests] == [
        ("workspace.list", {}),
        ("workspace.create", {"cwd": "/work", "label": "coban", "focus": False}),
    ]


async def test_find_session_looks_across_all_panes(
    herdr: tuple[StandInHerdr, HerdrPaneHost],
) -> None:
    stand_in, host = herdr
    panes = [{"pane_id": "w1:p2"}, PANE | {"pane_id": "w4:p1"}]
    stand_in.ok("pane.list", {"type": "pane_list", "panes": panes})

    assert await host.find_session("sess-1") == "w4:p1"
    assert await host.find_session("other") is None


async def test_watch_reads_again_on_every_event_and_resubscribes_after_lost_events(
    herdr: tuple[StandInHerdr, HerdrPaneHost],
) -> None:
    stand_in, host = herdr

    def show(status: str, rule: str) -> None:
        stand_in.ok("pane.get", {"type": "pane_info", "pane": PANE | {"agent_status": status}})
        explain = {"state": status, "matched_rule": {"id": rule}}
        stand_in.ok("agent.explain", {"type": "agent_explain", "explain": explain})

    show("idle", "live_prompt_box")
    stream = host.watch("w1:p6")
    assert (await asyncio.wait_for(anext(stream), 2)).state is AgentState.IDLE

    show("working", "osc_title_working")
    stand_in.events.put_nowait({"event": "pane.agent_status_changed", "data": {"pane_id": "w1:p6"}})
    assert (await asyncio.wait_for(anext(stream), 2)).state is AgentState.WORKING

    show("blocked", "bash_permission_prompt")
    stand_in.events.put_nowait({"error": {"code": "events_lost", "message": "lagged"}})
    # No event announced the change: the re-read after resubscribing finds it.
    assert (await asyncio.wait_for(anext(stream), 2)).state is AgentState.BLOCKED_OTHER
    assert stand_in.subscriptions == 2
    await stream.aclose()


async def test_an_unreachable_socket_is_reported_as_unavailable(tmp_path: Path) -> None:
    host = HerdrPaneHost(HerdrSocket(tmp_path / "missing.sock", timeout_s=1))

    with pytest.raises(PaneHostUnavailableError, match="cannot connect to herdr"):
        await host.observe("w1:p6")


async def test_a_server_that_never_answers_times_out(tmp_path: Path) -> None:
    async def silent(_reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        await asyncio.sleep(1)
        writer.close()

    socket_path = tmp_path / "herdr.sock"
    server = await asyncio.start_unix_server(silent, path=str(socket_path))
    async with server:
        host = HerdrPaneHost(HerdrSocket(socket_path, timeout_s=0.2))
        with pytest.raises(PaneHostUnavailableError, match=r"did not answer pane\.get"):
            await host.observe("w1:p6")
