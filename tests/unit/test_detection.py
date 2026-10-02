from __future__ import annotations

import re
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import TYPE_CHECKING

import pytest

from baton_herdr.core.detection import (
    DetectionRequest,
    DetectionResult,
    ScreenRule,
    bottom,
    detect,
)
from baton_herdr.core.model import AgentKind, AgentState
from baton_herdr.core.schemas import CONTRACTS, render

if TYPE_CHECKING:
    from zoneinfo import ZoneInfo

NOW = datetime(2026, 10, 2, 9, 0, tzinfo=UTC)
SCHEMAS = Path(__file__).parents[2] / "schemas"


def request(screen: str, host: AgentState = AgentState.UNKNOWN) -> DetectionRequest:
    return DetectionRequest(
        agent=AgentKind.CLAUDE,
        screen=screen,
        host_state=host,
        host_evidence="herdr:x",
        observed_at=NOW,
    )


def in_one_hour(_match: re.Match[str], after: datetime, _zone: ZoneInfo) -> datetime:
    return after + timedelta(hours=1)


LIMIT = ScreenRule(
    "limit", AgentState.RATE_LIMITED, re.compile(r"^LIMIT$", re.M), reset_parser=in_one_hour
)
GUESS_IDLE = ScreenRule(
    "prompt",
    AgentState.IDLE,
    re.compile(r"^>$", re.M),
    applies_when=frozenset({AgentState.UNKNOWN}),
)
RULES = [LIMIT, GUESS_IDLE]


def test_the_first_matching_rule_wins_and_reports_its_evidence() -> None:
    result = detect(request("working...\nLIMIT\n> \n"), RULES)
    assert result == DetectionResult(
        state=AgentState.RATE_LIMITED, evidence="baton:limit", resets_at=NOW + timedelta(hours=1)
    )


def test_guessing_rules_never_override_a_positive_host_state() -> None:
    assert detect(request("> \n", host=AgentState.UNKNOWN), RULES).state is AgentState.IDLE
    working = detect(request("> \n", host=AgentState.WORKING), RULES)
    assert (working.state, working.evidence) == (AgentState.WORKING, "herdr:x")


def test_without_a_match_the_host_conclusion_stands() -> None:
    result = detect(request("nothing here\n", host=AgentState.BLOCKED_OTHER), RULES)
    assert (result.state, result.evidence, result.resets_at) == (
        AgentState.BLOCKED_OTHER,
        "herdr:x",
        None,
    )


def test_rules_only_look_at_the_bottom_of_the_screen() -> None:
    old_limit_scrolled_away = "LIMIT\n" + "".join(f"line {i}\n" for i in range(40))
    assert detect(request(old_limit_scrolled_away), [LIMIT]).state is AgentState.UNKNOWN
    assert bottom("a\n\n  \nb  \nc\n", 2) == "b\nc"


def test_requests_and_results_round_trip_as_ndjson_lines() -> None:
    line = request("LIMIT\n").model_dump_json()
    assert "\n" not in line
    assert DetectionRequest.model_validate_json(line) == request("LIMIT\n")


@pytest.mark.parametrize("name", sorted(CONTRACTS))
def test_committed_schemas_match_the_models(name: str) -> None:
    """The files in schemas/ are the contract; regenerate them with `just schemas`."""
    assert (SCHEMAS / name).read_text(encoding="utf-8") == render(CONTRACTS[name])
