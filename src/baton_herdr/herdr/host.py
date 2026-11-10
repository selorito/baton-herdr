""":class:`baton_herdr.core.panes.PaneHost` backed by a running herdr server."""

from __future__ import annotations

import asyncio
from contextlib import suppress
from typing import TYPE_CHECKING, Any

from baton_herdr.core.model import AgentKind
from baton_herdr.core.panes import (
    AgentBlockedError,
    AgentNotRunningError,
    PaneHostError,
    PaneHostUnavailableError,
    PaneNotFoundError,
    PaneObservation,
    PaneProcess,
)
from baton_herdr.herdr.state import derive_state
from baton_herdr.herdr.transport import EventsLostError, HerdrApiError, HerdrUnavailableError

if TYPE_CHECKING:
    from collections.abc import AsyncGenerator, Callable, Mapping, Sequence

    from baton_herdr.herdr.fallback import ProcessFallback
    from baton_herdr.herdr.transport import HerdrSocket

_ERRORS: dict[str, type[PaneHostError]] = {
    "pane_not_found": PaneNotFoundError,
    "agent_not_found": AgentNotRunningError,
    "agent_not_running": AgentNotRunningError,
    "agent_blocked": AgentBlockedError,
}
_AGENT_KINDS = {kind.value: kind for kind in AgentKind}


DEFAULT_WORKSPACE_LABEL = "baton"
# The rows herdr's rules read, as the fixtures were captured (tools/capture).
EXPLAIN_SCREEN_LINES = 200
# herdr waits this long between a prompt's text and its Enter (src/app/api/agents.rs).
PROMPT_SUBMIT_DELAY_S = 0.3
# Reading a process's cmdline can hang on WSL; a probe slower than this finds nothing.
PROCESS_PROBE_TIMEOUT_S = 2.0


class HerdrPaneHost:
    """Panes are opened in a workspace of baton's own, never in the user's workspaces."""

    def __init__(
        self,
        socket: HerdrSocket,
        *,
        workspace_label: str = DEFAULT_WORKSPACE_LABEL,
        fallback: ProcessFallback | None = None,
    ) -> None:
        self._socket = socket
        self._workspace_label = workspace_label
        # Set where herdr cannot see a pane's processes (ADR 0022): baton then reads the
        # agent from the process table and the state from herdr's rules applied to a file.
        self._fallback = fallback
        # Tasks start side by side (ADR 0017); two first panes must not make two workspaces.
        self._opening = asyncio.Lock()

    async def observe(self, pane_id: str) -> PaneObservation:
        pane = (await self._call("pane.get", {"pane_id": pane_id}))["pane"]
        if self._fallback is not None:
            return await self._observe_from_processes(pane_id, pane, self._fallback)
        explain: dict[str, Any] | None = None
        if pane.get("agent") is not None:
            # The agent can exit between the two calls; that is "no agent", not a failure.
            with suppress(AgentNotRunningError):
                explain = (await self._call("agent.explain", {"target": pane_id}))["explain"]
        state, evidence = derive_state(pane.get("agent_status"), explain)
        session = pane.get("agent_session") or {}
        return PaneObservation(
            pane_id=pane_id,
            agent=_AGENT_KINDS.get(pane.get("agent") or "") if explain is not None else None,
            state=state,
            evidence=evidence,
            session_ref=session.get("value"),
            cwd=pane.get("foreground_cwd") or pane.get("cwd"),
        )

    async def _observe_from_processes(
        self, pane_id: str, pane: Mapping[str, Any], fallback: ProcessFallback
    ) -> PaneObservation:
        session = pane.get("agent_session") or {}
        agent: AgentKind | None = None
        explain: Mapping[str, Any] | None = None
        shell_pid = await self._shell_pid(pane_id)
        if shell_pid is not None:
            found = await _probe(lambda: fallback.agent(shell_pid))
            if found is not None:
                agent = found.kind
                screen = await self.read_screen(pane_id, lines=EXPLAIN_SCREEN_LINES)
                explain = await fallback.explain(agent, screen, pane.get("terminal_title"))
        # herdr's pane status follows its own detection, which sees no agent here.
        state, evidence = derive_state(None, explain)
        return PaneObservation(
            pane_id=pane_id,
            agent=agent,
            state=state,
            evidence=evidence,
            session_ref=session.get("value"),
            cwd=pane.get("foreground_cwd") or pane.get("cwd"),
        )

    async def _shell_pid(self, pane_id: str) -> int | None:
        info = (await self._call("pane.process_info", {"pane_id": pane_id}))["process_info"]
        pid = info.get("shell_pid")
        return pid if isinstance(pid, int) and pid > 0 else None

    async def watch(self, pane_id: str) -> AsyncGenerator[PaneObservation]:
        subscription = {"type": "pane.agent_status_changed", "pane_id": pane_id}
        while True:
            # herdr's events are invalidation signals. Subscribe first, then read: the
            # read is authoritative, and every later event triggers another read. The
            # same read reconciles state after herdr reports lost events.
            try:
                events = await self._socket.subscribe([subscription])
            except HerdrApiError as err:
                raise _translate(err) from err
            except HerdrUnavailableError as err:
                raise PaneHostUnavailableError(str(err)) from err
            try:
                yield await self.observe(pane_id)
                async for _event in events:
                    yield await self.observe(pane_id)
            except EventsLostError:
                continue
            except HerdrApiError as err:
                raise _translate(err) from err
            except HerdrUnavailableError as err:
                raise PaneHostUnavailableError(str(err)) from err
            finally:
                await events.aclose()

    async def read_screen(self, pane_id: str, *, lines: int = 200) -> str:
        params = {"pane_id": pane_id, "source": "recent", "lines": lines, "strip_ansi": True}
        text: str = (await self._call("pane.read", params))["read"]["text"]
        return text

    async def send_prompt(self, pane_id: str, text: str) -> None:
        if self._fallback is None:
            await self._call("agent.prompt", {"target": pane_id, "text": text})
            return
        # agent.prompt needs herdr to see the agent. Do what it does: check the agent can
        # take a prompt, paste the text, and press Enter a moment later.
        if not text:
            raise PaneHostError("agent prompt must not be empty", code="empty_agent_prompt")
        observation = await self.observe(pane_id)
        if observation.agent is None:
            msg = f"agent target {pane_id} not found"
            raise AgentNotRunningError(msg, code="agent_not_found")
        if observation.state.needs_human:
            msg = f"agent {pane_id} is blocked and requires interactive input"
            raise AgentBlockedError(msg, code="agent_blocked")
        await self._call("pane.send_input", {"pane_id": pane_id, "text": text})
        await asyncio.sleep(PROMPT_SUBMIT_DELAY_S)
        await self._call("pane.send_input", {"pane_id": pane_id, "keys": ["Enter"]})

    async def send_text(self, pane_id: str, text: str) -> None:
        await self._call("pane.send_text", {"pane_id": pane_id, "text": text})

    async def send_keys(self, pane_id: str, keys: Sequence[str]) -> None:
        await self._call("pane.send_keys", {"pane_id": pane_id, "keys": list(keys)})

    async def open_pane(self, *, cwd: str, label: str | None = None) -> str:
        """Open a pane in baton's workspace, creating the workspace on first use."""
        async with self._opening:
            return await self._open_pane(cwd=cwd, label=label)

    async def _open_pane(self, *, cwd: str, label: str | None) -> str:
        workspaces = (await self._call("workspace.list", {}))["workspaces"]
        own = next((w for w in workspaces if w.get("label") == self._workspace_label), None)
        if own is None:
            params = {"cwd": cwd, "label": self._workspace_label, "focus": False}
            result = await self._call("workspace.create", params)
        else:
            params = {
                "workspace_id": own["workspace_id"],
                "cwd": cwd,
                "label": label,
                "focus": False,
            }
            result = await self._call("tab.create", params)
        pane_id: str = result["root_pane"]["pane_id"]
        return pane_id

    async def close_pane(self, pane_id: str) -> None:
        await self._call("pane.close", {"pane_id": pane_id})

    async def find_session(self, session_ref: str) -> str | None:
        for pane in (await self._call("pane.list", {}))["panes"]:
            if (pane.get("agent_session") or {}).get("value") == session_ref:
                found: str = pane["pane_id"]
                return found
        return None

    async def processes(self, pane_id: str) -> Sequence[PaneProcess]:
        info = (await self._call("pane.process_info", {"pane_id": pane_id}))["process_info"]
        fallback = self._fallback
        shell_pid = info.get("shell_pid")
        if fallback is not None and isinstance(shell_pid, int) and shell_pid > 0:
            lines = await _probe(lambda: fallback.command_lines(shell_pid)) or []
            return [PaneProcess(pid=pid, command=command) for pid, command in lines]
        return [
            PaneProcess(pid=process["pid"], command=process.get("cmdline") or "")
            for process in info.get("foreground_processes", [])
        ]

    async def _call(self, method: str, params: Mapping[str, Any]) -> dict[str, Any]:
        try:
            return await self._socket.request(method, params)
        except HerdrApiError as err:
            raise _translate(err) from err
        except HerdrUnavailableError as err:
            raise PaneHostUnavailableError(str(err)) from err


async def _probe[T](read: Callable[[], T]) -> T | None:
    """``read()`` off the event loop; ``None`` if it does not finish in time."""
    try:
        return await asyncio.wait_for(asyncio.to_thread(read), PROCESS_PROBE_TIMEOUT_S)
    except TimeoutError:
        return None


def _translate(err: HerdrApiError) -> PaneHostError:
    return _ERRORS.get(err.code, PaneHostError)(err.message, code=err.code)
