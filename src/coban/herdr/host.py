""":class:`coban.core.panes.PaneHost` backed by a running herdr server."""

from __future__ import annotations

from contextlib import suppress
from typing import TYPE_CHECKING, Any

from coban.core.model import AgentKind
from coban.core.panes import (
    AgentBlockedError,
    AgentNotRunningError,
    PaneHostError,
    PaneHostUnavailableError,
    PaneNotFoundError,
    PaneObservation,
    PaneProcess,
)
from coban.herdr.state import derive_state
from coban.herdr.transport import EventsLostError, HerdrApiError, HerdrUnavailableError

if TYPE_CHECKING:
    from collections.abc import AsyncGenerator, Mapping, Sequence

    from coban.herdr.transport import HerdrSocket

_ERRORS: dict[str, type[PaneHostError]] = {
    "pane_not_found": PaneNotFoundError,
    "agent_not_found": AgentNotRunningError,
    "agent_not_running": AgentNotRunningError,
    "agent_blocked": AgentBlockedError,
}
_AGENT_KINDS = {kind.value: kind for kind in AgentKind}


DEFAULT_WORKSPACE_LABEL = "coban"


class HerdrPaneHost:
    """Panes are opened in a workspace of coban's own, never in the user's workspaces."""

    def __init__(
        self, socket: HerdrSocket, *, workspace_label: str = DEFAULT_WORKSPACE_LABEL
    ) -> None:
        self._socket = socket
        self._workspace_label = workspace_label

    async def observe(self, pane_id: str) -> PaneObservation:
        pane = (await self._call("pane.get", {"pane_id": pane_id}))["pane"]
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
        await self._call("agent.prompt", {"target": pane_id, "text": text})

    async def send_text(self, pane_id: str, text: str) -> None:
        await self._call("pane.send_text", {"pane_id": pane_id, "text": text})

    async def send_keys(self, pane_id: str, keys: Sequence[str]) -> None:
        await self._call("pane.send_keys", {"pane_id": pane_id, "keys": list(keys)})

    async def open_pane(self, *, cwd: str, label: str | None = None) -> str:
        """Open a pane in coban's workspace, creating the workspace on first use."""
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


def _translate(err: HerdrApiError) -> PaneHostError:
    return _ERRORS.get(err.code, PaneHostError)(err.message, code=err.code)
