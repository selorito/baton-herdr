"""The parity corpus for the screen classifier (ADR 0011, phase 2).

Cases cover every recorded capture under every host state, the limit messages the
adapters read (across zones and daylight-saving changes), the fake agents' screens,
and text that tests how lines and patterns behave at their edges. The Python detector
answers each one; the Rust classifier must give the same answers
(``tests/integration/test_detect_parity_binary.py``).

The corpus is built when the test runs, so it always reflects the current fixtures and
the current Python rules; nothing generated is committed.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from baton_herdr.adapters import ADAPTERS
from baton_herdr.core.detection import DetectionRequest, DetectionResult
from baton_herdr.core.detector import classify_with
from baton_herdr.core.model import AgentKind, AgentState
from baton_herdr.herdr.state import derive_state

import fake_agent

REPO = Path(__file__).parents[2]
FIXTURES = REPO / "fixtures"
SCRIPTS = REPO / "tools" / "fake-agent" / "scripts"

NOW = datetime(2026, 10, 1, 11, 0, tzinfo=UTC)  # a Thursday, 14:00 in Istanbul
# Moments around daylight-saving changes, as UTC instants.
OBSERVED = {
    "now": "2026-10-01T11:00:00Z",
    "offset": "2026-10-01T14:00:00.123456+03:00",
    "berlin-spring-eve": "2026-03-28T23:00:00Z",  # 00:00 in Berlin; 02:00-03:00 skipped
    "berlin-fall-overlap": "2026-10-24T23:30:00Z",  # 01:30 CEST; 02:00-03:00 happens twice
    "ny-spring-eve": "2026-03-08T06:30:00Z",  # 01:30 EST; 02:00-03:00 skipped
    "ny-fall-overlap": "2026-11-01T05:30:00Z",  # 01:30 EDT; 01:00-02:00 happens twice
}
ZONES = (
    "UTC",
    "Europe/Istanbul",
    "Europe/Berlin",
    "America/New_York",
    "US/Eastern",
    "Asia/Kolkata",
    "Australia/Lord_Howe",
)

CLAUDE_LIMITS = (
    "You've hit your session limit · resets 3:45pm",
    "You've hit your session limit · resets 1pm",
    "You've hit your session limit · resets 12:00am",
    "You've hit your session limit · resets 12am",
    "You've hit your session limit · resets 2:30am",
    "You've hit your session limit · resets 1:30am",
    "You've hit your session limit · resets 2:30 A.M.",
    "You've hit your session limit · resets 11:59 p.m.",
    "You've hit your session limit · resets 0:30am",
    "You've hit your weekly limit · resets Mon 12:00am",
    "You've hit your weekly limit · resets Sunday 2:30am",
    "You've hit your weekly limit · resets Thu 9am",
    "You've hit your Opus limit · your weekly limit resets Fri 3pm",
    "You've hit your session limit · resets 3:45pm (America/New_York)",
    "You've hit your session limit · resets 3:45pm (Asia/Kolkata)",
    "You've hit your session limit · resets 3:45pm (america/new_york)",
    "You've hit your session limit · resets 3:45pm (Not/AZone)",
    "YOU'VE HIT YOUR SESSION LIMIT · RESETS 3:45PM",
    "You've hit your monthly spend limit",
    "You've hit your org's usage budget",
    "You've used 85% of your session limit · resets 3:45pm",
    "Context limit reached · /compact or /clear to continue",
)
CODEX_LIMITS = (
    "■ You've hit your usage limit. Upgrade to Pro (https://chatgpt.com/explore/pro), visit "
    "https://chatgpt.com/codex/settings/usage to purchase more credits or try again at "
    "Feb 23rd, 2026 9:01 PM.",
    "■ You've hit your usage limit. Try again at Mar 29th, 2026 2:30 AM.",
    "■ You've hit your usage limit. Try again at Nov 1st, 2026 1:30 AM.",
    "■ You've hit your usage limit. Try again at Sept 5th 2026 9:01 PM.",
    "■ You've hit your usage limit. Try again at Feb 30th, 2026 9:01 PM.",
    "■ You've hit your usage limit. Try again at Jun 1st, 0000 9:01 PM.",
    "■ You've hit your usage limit. Upgrade to Pro (https://openai.com/chatgpt/pricing) or "
    "try again in 3 hours 2 minutes.",
    "■ You've hit your usage limit. Try again in 4 days 2 hours 46 minutes.",
    "■ You've hit your usage limit. Try again in 45 min.",
    "■ You've hit your usage limit. Try again in 1 hr 1 sec.",
    "■ You've hit your usage limit. Upgrade to Plus to continue using Codex",
    "■ You've hit your usage limit. To get more access now, send a request to your\n"
    "admin or try again at Apr 12th, 2026\n3:31 PM.",
)
OPENCODE_LIMITS = (
    "Free limit reached",
    "Go usage limit reached. It will reset in 2 hours 15 minutes. To continue using this "
    "model now, enable usage from your available balance",
    "Go usage limit reached. It will reset in 3h 5m.",
    "Too Many Requests",
)
EDGES: dict[str, tuple[AgentKind, str]] = {
    "empty": (AgentKind.CLAUDE, ""),
    "blank": (AgentKind.CODEX, " \n\t\n　\n"),
    "crlf-limit": (AgentKind.CLAUDE, "out\r\nYou've hit your session limit · resets 3pm\r\n❯\r\n"),
    "separators": (
        AgentKind.CODEX,
        "a\x0cb\x1cc › Ask Codex to do anything   gpt · ~/w\x0b",
    ),
    "trailing-unicode-space": (
        AgentKind.CODEX,
        "› Ask Codex to do anything　\n  gpt · ~/dev/baton-sandbox  \n",
    ),
    "codex-option-is-not-a-composer": (AgentKind.CODEX, "› 1. Yes, proceed\n  gpt · ~/w\n"),
    "codex-spinner-footer": (AgentKind.CODEX, "› Ask Codex\n  gpt · ~/dev · ⠏\n"),
    "opencode-progress-after-composer": (
        AgentKind.OPENCODE,
        "  ┃  Build · Big Pickle OpenCode Zen\n  ■⬝⬝⬝ esc interrupt\n",
    ),
    "opencode-progress-between-composers": (
        AgentKind.OPENCODE,
        "  ┃  Build · a\n  ■⬝⬝ esc interrupt\n  ┃  Plan · b\n",
    ),
    "opencode-sessions": (AgentKind.OPENCODE, "  Sessions  esc\n\n  x\n  y\n  Search\n"),
    "opencode-sessions-too-far": (AgentKind.OPENCODE, "Sessions\na\nb\nc\nd\nSearch\n"),
    "claude-picker": (
        AgentKind.CLAUDE,
        "Resume Session\nCtrl+A to show all projects · Space to preview · Enter to select\n",
    ),
    "codex-trust": (
        AgentKind.CODEX,
        "Trust this folder?\n\n› 1. Trust and continue\n  2. Quit\n",
    ),
    "codex-approval": (
        AgentKind.CODEX,
        "Allow command?\n  rm -rf build\nPress enter to confirm or esc to cancel\n",
    ),
    "long-line": (AgentKind.CLAUDE, "x" * 5000 + "\nYou've hit your session limit"),
    "word-characters": (AgentKind.CLAUDE, "You've hit your naïve_ünïcode limit · resets 3pm"),
}
HOST_STATES = tuple(AgentState)


def python_classify(request: DetectionRequest) -> DetectionResult:
    """What ``baton detect`` answers."""
    return classify_with(ADAPTERS, request)


def _case(name: str, request: dict[str, Any], screen_file: str | None = None) -> dict[str, Any]:
    case: dict[str, Any] = {"name": name, "request": request}
    if screen_file is not None:
        case["screen_file"] = screen_file
    return case


def _captures() -> list[dict[str, Any]]:
    cases: list[dict[str, Any]] = []
    for explain_path in sorted(FIXTURES.glob("*/*/*/explain.json")):
        directory = explain_path.parent
        capture = str(directory.relative_to(FIXTURES))
        agent = capture.split("/", maxsplit=1)[0]
        pane = json.loads((directory / "pane.json").read_text())
        explain = json.loads(explain_path.read_text())
        running = "error" not in explain
        host_state, host_evidence = derive_state(
            pane.get("agent_status"), explain if running else None
        )
        for source in ("screen.detection.txt", "screen.txt"):
            if not (directory / source).is_file():
                continue
            screen_file = str((directory / source).relative_to(REPO))
            base = {"agent": agent, "observed_at": OBSERVED["now"], "timezone": "Europe/Istanbul"}
            cases.append(
                _case(
                    f"capture:{capture}:{source}:as-recorded",
                    base
                    | {
                        "host_state": host_state.value,
                        "host_evidence": host_evidence,
                        "agent_running": running,
                    },
                    screen_file,
                )
            )
            cases.extend(
                _case(
                    f"capture:{capture}:{source}:host={state.value}",
                    base | {"host_state": state.value},
                    screen_file,
                )
                for state in HOST_STATES
            )
            cases.append(
                _case(
                    f"capture:{capture}:{source}:no-agent",
                    base | {"agent_running": False, "host_evidence": "herdr:no-agent"},
                    screen_file,
                )
            )
    return cases


def _limits() -> list[dict[str, Any]]:
    """Every message once; those with a clock or a date also at every moment and zone."""
    groups = (
        (AgentKind.CLAUDE, CLAUDE_LIMITS, ("❯",)),
        (AgentKind.CODEX, CODEX_LIMITS, ("› Ask Codex to do anything", "  gpt · ~/w")),
        (AgentKind.OPENCODE, OPENCODE_LIMITS, ("  ┃  Build · Big Pickle OpenCode Zen",)),
    )
    cases: list[dict[str, Any]] = []
    for agent, lines, below in groups:
        for number, line in enumerate(lines):
            text = line if agent is not AgentKind.OPENCODE else f"  ┃  {line}"
            screen = "\n".join(["previous output", text, *below, ""])
            local_time = " resets " in line.lower() or " again at " in line.lower()
            moments = OBSERVED if local_time else {k: OBSERVED[k] for k in ("now", "offset")}
            cases.extend(
                _case(
                    f"limit:{agent.value}:{number}:{observed}:{zone}",
                    {
                        "agent": agent.value,
                        "screen": screen,
                        "host_state": "idle",
                        "observed_at": at,
                        "timezone": zone,
                    },
                )
                for observed, at in moments.items()
                for zone in (ZONES if local_time else ("UTC",))
            )
    return cases


def _fake_agents() -> list[dict[str, Any]]:
    cases: list[dict[str, Any]] = []
    for script_path in sorted(SCRIPTS.glob("*.toml")):
        script = fake_agent.load_script(script_path)
        for number, step in enumerate(script.steps):
            screen = fake_agent.render(step.screen, now=NOW, prompt="Add a power function.")
            cases.extend(
                _case(
                    f"fake:{script_path.stem}:{number}:{host.value}",
                    {
                        "agent": script.agent,
                        "screen": screen,
                        "host_state": host.value,
                        "observed_at": OBSERVED["now"],
                        "timezone": "UTC",
                    },
                )
                for host in (AgentState.UNKNOWN, AgentState.IDLE, AgentState.WORKING)
            )
    return cases


def _edges() -> list[dict[str, Any]]:
    cases = [
        _case(
            f"edge:{name}:{host.value}",
            {
                "agent": agent.value,
                "screen": screen,
                "host_state": host.value,
                "observed_at": OBSERVED["now"],
            },
        )
        for name, (agent, screen) in EDGES.items()
        for host in (AgentState.UNKNOWN, AgentState.IDLE)
    ]
    # herdr's blocked rule ids turned into kinds (Claude), or left alone (Codex).
    cases.extend(
        _case(
            f"refine:{agent.value}:{rule}",
            {
                "agent": agent.value,
                "screen": "plain text",
                "host_state": "blocked_other",
                "host_evidence": f"herdr:rule:{rule}",
                "observed_at": OBSERVED["now"],
            },
        )
        for agent in (AgentKind.CLAUDE, AgentKind.CODEX, AgentKind.OPENCODE)
        for rule in ("bash_permission_prompt", "live_blocked_form", "something_else")
    )
    return cases


def cases() -> list[dict[str, Any]]:
    return [*_captures(), *_limits(), *_fake_agents(), *_edges()]


def request_of(case: dict[str, Any]) -> DetectionRequest:
    payload = dict(case["request"])
    if "screen_file" in case:
        payload["screen"] = (REPO / case["screen_file"]).read_text(encoding="utf-8")
    return DetectionRequest.model_validate(payload)
