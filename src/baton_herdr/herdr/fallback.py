"""Reading a pane's agent when herdr cannot see the pane's processes (ADR 0022).

herdr learns which agent runs in a pane from the terminal's foreground process group
(``tpgid`` in ``/proc/<pid>/stat``) or, in its ``child-groups`` mode, from
``/proc/<pid>/task/<tid>/children``. WSL1 provides neither: ``tpgid`` is always 0 and the
``children`` files do not exist. herdr then reports no agent in any pane, and the calls that
need one (``agent.explain``, ``agent.prompt``) answer ``agent_not_found``.

baton runs on the same machine as herdr, so it can read the process table itself: the pane's
shell pid comes from ``pane.process_info``, the shell's descendants from the parent pid in
``/proc/<pid>/stat``. The screen is classified by herdr's own rules through
``herdr agent explain --file``, which needs no process; herdr's rules on the terminal title,
which a file cannot carry, are applied here from herdr's own manifests.
"""

from __future__ import annotations

import asyncio
import json
import os
import re
import tempfile
import tomllib
from contextlib import suppress
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any

from baton_herdr.core.model import AgentKind
from baton_herdr.core.processes import ProcessEntry

if TYPE_CHECKING:
    from collections.abc import Callable, Mapping, Sequence

    from baton_herdr.core.processes import ProcessTable, ScreenExplainer

_AGENT_NAMES = {kind.value: kind for kind in AgentKind}
# Programs that run an agent's script: the agent's name is the script's (herdr does the same,
# src/detect/mod.rs normalized_process_name).
_RUNTIMES = frozenset({"node", "bun", "deno", "python", "python3"})
# A process in these states may hang a read of its cmdline on WSL (herdr src/platform/linux.rs).
_NO_CMDLINE_STATES = frozenset({"Z", "X", "x"})
DEFAULT_EXPLAIN_TIMEOUT_S = 5.0


@dataclass(frozen=True, slots=True)
class PaneAgent:
    kind: AgentKind
    pid: int


class ProcessFallback:
    """What :class:`~baton_herdr.herdr.host.HerdrPaneHost` asks instead of herdr's detection."""

    def __init__(
        self,
        table: ProcessTable,
        explainer: ScreenExplainer,
        title_rules: Callable[[AgentKind], Sequence[TitleRule]] | None = None,
    ) -> None:
        self._table = table
        self._explainer = explainer
        self._title_rules = title_rules or builtin_title_rules

    def descendants(self, root_pid: int) -> list[ProcessEntry]:
        """``root_pid``'s descendants, nearest first."""
        children: dict[int, list[ProcessEntry]] = {}
        for entry in self._table.entries():
            children.setdefault(entry.ppid, []).append(entry)
        found: list[ProcessEntry] = []
        level = [root_pid]
        seen = {root_pid}
        while level:
            following: list[int] = []
            for pid in level:
                for child in sorted(children.get(pid, []), key=lambda e: e.pid):
                    if child.pid not in seen:
                        seen.add(child.pid)
                        found.append(child)
                        following.append(child.pid)
            level = following
        return found

    def agent(self, shell_pid: int) -> PaneAgent | None:
        """The agent nearest to the pane's shell, if one runs under it."""
        for entry in self.descendants(shell_pid):
            kind = identify(entry, self._argv_reader(entry))
            if kind is not None:
                return PaneAgent(kind=kind, pid=entry.pid)
        return None

    def command_lines(self, shell_pid: int) -> list[tuple[int, str]]:
        """The pane's processes with their command lines: its descendants, else the shell."""
        entries = self.descendants(shell_pid)
        if not entries:
            entries = [e for e in self._table.entries() if e.pid == shell_pid]
        lines = []
        for entry in entries:
            argv = self._argv_reader(entry)()
            lines.append((entry.pid, " ".join(argv) if argv else entry.comm))
        return lines

    async def explain(self, agent: AgentKind, screen: str, title: str | None) -> Mapping[str, Any]:
        """herdr's verdict on the pane: its screen rules from a file, its title rules here."""
        screen_explain = await self._explainer.explain(agent, screen)
        return with_title(screen_explain, title, self._title_rules(agent))

    def _argv_reader(self, entry: ProcessEntry) -> Callable[[], Sequence[str] | None]:
        def read() -> Sequence[str] | None:
            if entry.state in _NO_CMDLINE_STATES:
                return None
            return self._table.argv(entry.pid)

        return read


def identify(
    entry: ProcessEntry, read_argv: Callable[[], Sequence[str] | None]
) -> AgentKind | None:
    """The agent ``entry`` is, by its name or by the script its runtime runs."""
    by_name = _AGENT_NAMES.get(entry.comm.lower())
    if by_name is not None:
        # The name is enough; reading cmdline is what can hang on WSL.
        return by_name
    argv = read_argv()
    if not argv:
        return None
    first = _basename(argv[0])
    if first in _AGENT_NAMES:
        return _AGENT_NAMES[first]
    if first.rstrip("0123456789.") in _RUNTIMES:
        script = next((arg for arg in argv[1:] if not arg.startswith("-")), None)
        if script is not None:
            return _AGENT_NAMES.get(_basename(script).removesuffix(".js"))
    return None


def _basename(path: str) -> str:
    return path.rsplit("/", 1)[-1].lower()


# --- herdr's rules on the terminal title ------------------------------------------------
#
# `explain --file` reads only the screen; herdr's rules on the terminal title (region
# "osc_title") are applied here, ranked against the screen's rule by priority as herdr does
# (src/detect/manifest.rs evaluate_loaded_manifest). They are read from the manifests herdr
# keeps up to date, so a rule herdr changes is followed; BUILTIN_TITLE_RULES stands in until
# herdr has fetched one.

TITLE_REGION = "osc_title"
BUILTIN_TITLE_RULES = """
[[claude]]
id = "osc_title_working"
state = "working"
priority = 1100
regex = ['^[\\x{2800}-\\x{28FF}\\x{25D0}-\\x{25D3}] ']

[[claude]]
id = "osc_title_idle"
state = "idle"
priority = 250
regex = ['^\\x{2733} ']

[[codex]]
id = "osc_title_blocked"
state = "blocked"
priority = 1100
contains = ["Action Required"]

[[codex]]
id = "osc_title_working"
state = "working"
priority = 1050
regex = ['(?:^| )[⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏](?: |$)']

[[codex]]
id = "osc_title_idle"
state = "idle"
priority = 100
regex = ['\\S']
not = [{ regex = ['(?:^| )[⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏](?: |$)'] }, { contains = ["Action Required"] }]
"""  # herdr manifests 2026.10.11.1


@dataclass(frozen=True, slots=True)
class Gate:
    contains: tuple[str, ...] = ()
    regex: tuple[re.Pattern[str], ...] = ()
    line_regex: tuple[re.Pattern[str], ...] = ()
    all: tuple[Gate, ...] = ()
    any: tuple[Gate, ...] = ()
    not_: tuple[Gate, ...] = ()

    def matches(self, text: str) -> bool:
        """herdr's compiled_gate_matches: every needle, regex and ``all`` gate; one ``any``
        gate if there are any; no ``not`` gate."""
        lower = text.lower()
        return (
            all(needle in lower for needle in self.contains)
            and all(pattern.search(text) for pattern in self.regex)
            and all(any(p.search(line) for line in text.splitlines()) for p in self.line_regex)
            and all(gate.matches(text) for gate in self.all)
            and (not self.any or any(gate.matches(text) for gate in self.any))
            and not any(gate.matches(text) for gate in self.not_)
        )


@dataclass(frozen=True, slots=True)
class TitleRule:
    id: str
    state: str
    priority: int
    gate: Gate


_RUST_HEX = re.compile(r"\\x\{([0-9A-Fa-f]{1,6})\}")


def _pattern(rust: str) -> re.Pattern[str]:
    """A Rust regex as Python's: ``\\x{2800}`` is written ``\\U00002800``."""
    return re.compile(_RUST_HEX.sub(lambda m: f"\\U{int(m.group(1), 16):08x}", rust))


def _gate(spec: Mapping[str, Any]) -> Gate:
    return Gate(
        contains=tuple(str(needle).lower() for needle in spec.get("contains", ())),
        regex=tuple(_pattern(p) for p in spec.get("regex", ())),
        line_regex=tuple(_pattern(p) for p in spec.get("line_regex", ())),
        all=tuple(_gate(g) for g in spec.get("all", ())),
        any=tuple(_gate(g) for g in spec.get("any", ())),
        not_=tuple(_gate(g) for g in spec.get("not", ())),
    )


def title_rules_from(specs: Sequence[Mapping[str, Any]]) -> tuple[TitleRule, ...]:
    """The title rules among a manifest's ``[[rules]]``; a rule that does not compile is
    left out, as herdr leaves out a manifest that does not."""
    rules = []
    for spec in specs:
        try:
            rules.append(
                TitleRule(
                    id=str(spec["id"]),
                    state=str(spec["state"]),
                    priority=int(spec.get("priority", 0)),
                    gate=_gate(spec),
                )
            )
        except (KeyError, TypeError, ValueError, re.error):
            continue
    return tuple(rules)


def builtin_title_rules(agent: AgentKind) -> tuple[TitleRule, ...]:
    return title_rules_from(tomllib.loads(BUILTIN_TITLE_RULES).get(agent.value, []))


def herdr_state_dir(env: Mapping[str, str], home: Path) -> Path:
    """herdr's state directory (src/config/io.rs state_dir)."""
    state_home = env.get("XDG_STATE_HOME")
    return (Path(state_home) if state_home else home / ".local" / "state") / "herdr"


class ManifestTitleRules:
    """Title rules read from the manifests herdr fetched, else the built-in copy."""

    def __init__(self, state_dir: Path) -> None:
        self._remote = state_dir / "agent-detection" / "remote"
        self._cache: dict[AgentKind, tuple[float, tuple[TitleRule, ...]]] = {}

    def __call__(self, agent: AgentKind) -> tuple[TitleRule, ...]:
        path = self._remote / f"{agent.value}.toml"
        try:
            mtime = path.stat().st_mtime
        except OSError:
            return builtin_title_rules(agent)
        cached = self._cache.get(agent)
        if cached is not None and cached[0] == mtime:
            return cached[1]
        try:
            manifest = tomllib.loads(path.read_text(encoding="utf-8"))
        except (OSError, tomllib.TOMLDecodeError):
            return builtin_title_rules(agent)
        specs = [r for r in manifest.get("rules", []) if r.get("region") == TITLE_REGION]
        rules = title_rules_from(specs)
        self._cache[agent] = (mtime, rules)
        return rules


def with_title(
    screen_explain: Mapping[str, Any], title: str | None, rules: Sequence[TitleRule]
) -> Mapping[str, Any]:
    """The screen's explain, unless a title rule of higher priority matches the title."""
    if not title:
        return screen_explain
    matched = max(
        (rule for rule in rules if rule.gate.matches(title)),
        key=lambda rule: rule.priority,
        default=None,
    )
    if matched is None:
        return screen_explain
    screen_rule = screen_explain.get("matched_rule") or {}
    screen_priority = screen_rule.get("priority")
    if screen_rule and (screen_priority is None or screen_priority >= matched.priority):
        return screen_explain
    return {
        "state": matched.state,
        "matched_rule": {"id": matched.id, "priority": matched.priority, "region": TITLE_REGION},
        "fallback_reason": None,
    }


def running_on_wsl1(osrelease: str | None = None) -> bool:
    """True on WSL1, whose kernel release reads like ``4.4.0-26100-Microsoft``.

    WSL2's (``…-microsoft-standard-WSL2``) is a real Linux kernel herdr can read.
    """
    if osrelease is None:
        try:
            osrelease = Path("/proc/sys/kernel/osrelease").read_text(encoding="utf-8")
        except OSError:
            return False
    release = osrelease.strip()
    return release.endswith("-Microsoft") and "WSL2" not in release


class ProcProcessTable:
    """:class:`ProcessTable` read from ``/proc``."""

    def __init__(self, root: Path = Path("/proc")) -> None:
        self._root = root

    def entries(self) -> list[ProcessEntry]:
        found = []
        for path in self._root.iterdir():
            if not path.name.isdigit():
                continue
            try:
                stat = (path / "stat").read_text(encoding="utf-8", errors="replace")
            except OSError:
                continue  # exited while listing
            entry = parse_stat(stat)
            if entry is not None:
                found.append(entry)
        return found

    def argv(self, pid: int) -> list[str] | None:
        try:
            raw = (self._root / str(pid) / "cmdline").read_bytes()
        except OSError:
            return None
        parts = [part.decode(errors="replace") for part in raw.split(b"\0") if part]
        return parts or None


def parse_stat(stat: str) -> ProcessEntry | None:
    """``pid (comm) state ppid …``; comm may itself hold spaces and parentheses."""
    open_paren, close_paren = stat.find("("), stat.rfind(")")
    if open_paren < 0 or close_paren < open_paren:
        return None
    fields = stat[close_paren + 2 :].split()
    try:
        return ProcessEntry(
            pid=int(stat[:open_paren]),
            ppid=int(fields[1]),
            comm=stat[open_paren + 1 : close_paren],
            state=fields[0],
        )
    except (IndexError, ValueError):
        return None


class HerdrFileExplainer:
    """:class:`ScreenExplainer` running ``herdr agent explain --file … --agent … --json``."""

    def __init__(self, binary: str = "herdr", *, timeout_s: float = DEFAULT_EXPLAIN_TIMEOUT_S):
        self._binary = binary
        self._timeout_s = timeout_s

    async def explain(self, agent: AgentKind, screen: str) -> dict[str, Any]:
        fd, name = tempfile.mkstemp(prefix="baton-screen-", suffix=".txt")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as file:
                file.write(screen)
            process = await asyncio.create_subprocess_exec(
                self._binary,
                "agent",
                "explain",
                "--file",
                name,
                "--agent",
                agent.value,
                "--json",
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.DEVNULL,
            )
            try:
                out, _ = await asyncio.wait_for(process.communicate(), self._timeout_s)
            except TimeoutError:
                process.kill()
                await process.wait()
                return {}
        finally:
            with suppress(FileNotFoundError):
                os.unlink(name)  # noqa: PTH108 - pathlib here would block the loop (ASYNC240)
        if process.returncode != 0:
            return {}
        try:
            payload = json.loads(out)
        except json.JSONDecodeError:
            return {}
        return payload if isinstance(payload, dict) else {}
