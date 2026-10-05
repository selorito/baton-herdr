"""The runner lets the policy answer permission prompts (ADR 0013), on simulated agents."""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

from baton_herdr.adapters import ADAPTERS
from baton_herdr.core.events import OperatorActed, PermissionDecided, TaskCreated
from baton_herdr.core.fakes import FixedClock, InMemoryEventStore, RecordingNotifier
from baton_herdr.core.model import AgentKind, PermissionDecision, TaskId, TaskStatus
from baton_herdr.core.notify import NoticeKind
from baton_herdr.policy.defaults import DEFAULT_RULES
from baton_herdr.policy.load import Policy
from baton_herdr.policy.rules import Rule
from baton_herdr.scheduler.runner import RunnerSettings, TaskRunner

from simulated_agents import SimulatedAgents

NOW = datetime(2026, 10, 5, 9, 0, tzinfo=UTC)
TASK = TaskId("power")
SETTINGS = RunnerSettings(
    agents=(AgentKind.CLAUDE,),
    start_timeout_s=2,
    turn_timeout_s=2,
    poll_interval_s=0.05,
    usage_grace_s=0,
)
# The recorded prompt's shape: the command, then its description.
TESTS = (
    "────────────────\n Bash command\n\n   python3 -m unittest -v 2>&1\n"
    "   Run the unit test suite verbosely\n\n Do you want to proceed?\n ❯ 1. Yes\n"
)
DELETE = "────────────────\n Bash command\n\n   rm -rf build\n   Remove the build directory\n\n Do you want to proceed?\n"  # noqa: E501
DONE = "⏺ Done.\n  [[BATON:END status=done]]"


async def run(
    screen: str, policy: Policy | None, end_mark: str | None = DONE
) -> tuple[InMemoryEventStore, SimulatedAgents, RecordingNotifier, TaskStatus]:
    store = InMemoryEventStore()
    await store.append(
        [
            TaskCreated(
                occurred_at=NOW,
                task_id=TASK,
                title="power function",
                instructions="Add power(a, b).",
                workdir="/work/calc",
            )
        ]
    )
    clock = FixedClock(NOW)
    host = SimulatedAgents(
        clock,
        scripts={AgentKind.CLAUDE: "claude-finish.toml"},
        permission_after_prompt=True,
        permission_screen=screen,
        end_mark=end_mark,
    )
    notifier = RecordingNotifier()
    runner = TaskRunner(
        store=store,
        host=host,
        adapters=ADAPTERS,
        notifier=notifier,
        clock=clock,
        settings=SETTINGS,
        policy=policy,
    )
    return store, host, notifier, await runner.run(TASK)


def decisions(store: InMemoryEventStore) -> list[PermissionDecided]:
    return [s.event for s in store._events if isinstance(s.event, PermissionDecided)]


TRUSTED = Policy(trusted_dirs=(Path("/work/calc"),))


async def test_an_allowed_command_is_approved_without_a_person() -> None:
    store, host, notifier, status = await run(TESTS, TRUSTED)

    assert status is TaskStatus.COMPLETED
    assert NoticeKind.NEEDS_HUMAN not in notifier.kinds
    assert host.permission_answers == [("Enter",)]  # once, though the prompt showed a while
    (decided,) = decisions(store)
    assert decided.decision is PermissionDecision.ALLOW
    assert decided.command == "python3 -m unittest -v 2>&1"
    assert decided.rule == "python3 -m unittest* (defaults)"
    assert decided.reason == "python3 -m unittest -v: runs the project's own code (tests, builds)"
    (acted,) = [s.event for s in store._events if isinstance(s.event, OperatorActed)]
    assert acted.by == "policy"
    assert acted.blocker_seq == decided.blocker_seq


async def test_tests_outside_trusted_dirs_go_to_a_person() -> None:
    store, host, notifier, status = await run(TESTS, Policy())

    assert status is TaskStatus.NEEDS_HUMAN
    assert host.permission_answers == []
    (decided,) = decisions(store)
    assert decided.decision is PermissionDecision.ASK
    assert "/work/calc is not one" in decided.reason


async def test_a_risky_command_goes_to_a_person_with_the_reason() -> None:
    store, host, notifier, status = await run(DELETE, Policy())

    assert status is TaskStatus.NEEDS_HUMAN
    assert host.permission_answers == []
    asked = notifier.notices[-1]
    assert asked.kind is NoticeKind.NEEDS_HUMAN
    assert asked.details == ("Policy: ask, rm -rf build: deletes files.",)
    (decided,) = decisions(store)
    assert decided.decision is PermissionDecision.ASK


async def test_a_denied_command_is_refused_and_the_agent_waits_for_instructions() -> None:
    policy = Policy(
        rules=(
            Rule(PermissionDecision.DENY, "rm -rf *", "never in this repo", origin="test"),
            *DEFAULT_RULES,
        )
    )
    # A refused agent stops without an end mark.
    store, host, notifier, status = await run(DELETE, policy, end_mark=None)

    assert host.permission_answers == [("Escape",)]
    assert status is TaskStatus.NEEDS_HUMAN
    asked = notifier.notices[-1]
    assert "denied" in asked.text
    assert asked.details == ("Policy: deny, rm -rf build: never in this repo.",)
    (decided,) = decisions(store)
    assert decided.rule == "rm -rf * (test)"


async def test_without_a_policy_every_prompt_goes_to_a_person() -> None:
    store, host, _, status = await run(TESTS, None)

    assert status is TaskStatus.NEEDS_HUMAN
    assert host.permission_answers == []
    assert decisions(store) == []
