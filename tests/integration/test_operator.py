"""Operator actions (ADR 0009) on simulated agents: approve, answer, refusals, and the
Telegram bot's logic without Telegram (owner lock, status, replies, buttons)."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from baton_herdr.adapters import ADAPTERS
from baton_herdr.core.events import OperatorActed, TaskCreated
from baton_herdr.core.fakes import FixedClock, InMemoryEventStore, RecordingNotifier
from baton_herdr.core.model import AgentKind, OperatorAction, TaskId, TaskStatus
from baton_herdr.core.notify import NoticeKind
from baton_herdr.scheduler.operator import ActionRefusedError, act
from baton_herdr.scheduler.runner import RunnerSettings, TaskRunner
from baton_herdr.telegram.bot import HELP, BotSettings, OperatorBot
from baton_herdr.telegram.notifier import button_data, format_notice

from simulated_agents import SimulatedAgents

NOW = datetime(2026, 10, 3, 9, 0, tzinfo=UTC)
TASK = TaskId("power")
CLOCK = FixedClock(NOW)
SETTINGS = RunnerSettings(
    agents=(AgentKind.CLAUDE,),
    start_timeout_s=2,
    turn_timeout_s=2,
    poll_interval_s=0.05,
)
QUESTION = "⏺ Should power(0, 0) return 1 or raise?\n  [[BATON:END status=question]]"
DONE = "⏺ Done.\n  [[BATON:END status=done]]"


async def setup(
    **simulation: object,
) -> tuple[InMemoryEventStore, SimulatedAgents, RecordingNotifier, TaskRunner]:
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
    host = SimulatedAgents(
        CLOCK,
        scripts={AgentKind.CLAUDE: "claude-finish.toml"},
        **simulation,  # type: ignore[arg-type]
    )
    notifier = RecordingNotifier()
    runner = TaskRunner(
        store=store, host=host, adapters=ADAPTERS, notifier=notifier, clock=CLOCK, settings=SETTINGS
    )
    return store, host, notifier, runner


async def do(
    store: InMemoryEventStore,
    host: SimulatedAgents,
    action: OperatorAction,
    *,
    blocker_seq: int | None,
    text: str = "",
) -> None:
    await act(
        store=store,
        host=host,
        adapters=ADAPTERS,
        clock=CLOCK,
        task_id=TASK,
        action=action,
        by="telegram:42",
        blocker_seq=blocker_seq,
        text=text,
    )


async def test_a_permission_prompt_is_approved_once_from_its_notice() -> None:
    store, host, notifier, runner = await setup(permission_after_prompt=True)
    assert await runner.run(TASK) is TaskStatus.NEEDS_HUMAN

    (asked,) = [n for n in notifier.notices if n.kind is NoticeKind.NEEDS_HUMAN]
    assert asked.actions == (OperatorAction.APPROVE, OperatorAction.DENY)
    assert asked.blocker_seq is not None

    await do(store, host, OperatorAction.APPROVE, blocker_seq=asked.blocker_seq)
    assert (".", "keys", ("Enter",)) in [(".", kind, v) for _, kind, v in host.sent]
    acted = [s.event for s in await store.read() if isinstance(s.event, OperatorActed)]
    assert [(e.action, e.blocker_seq, e.by) for e in acted] == [
        (OperatorAction.APPROVE, asked.blocker_seq, "telegram:42")
    ]
    # The same button again does nothing: the blocker is answered, or gone.
    with pytest.raises(ActionRefusedError):
        await do(store, host, OperatorAction.APPROVE, blocker_seq=asked.blocker_seq)

    assert await runner.run(TASK) is TaskStatus.COMPLETED


async def test_a_question_is_answered_with_free_text() -> None:
    store, host, notifier, runner = await setup(end_mark=QUESTION)
    assert await runner.run(TASK) is TaskStatus.NEEDS_HUMAN
    (asked,) = [n for n in notifier.notices if n.kind is NoticeKind.NEEDS_HUMAN]
    assert asked.actions == (OperatorAction.ANSWER,)

    # Approving a question is refused, and so is an empty answer.
    with pytest.raises(ActionRefusedError, match="does not fit"):
        await do(store, host, OperatorAction.APPROVE, blocker_seq=asked.blocker_seq)
    with pytest.raises(ActionRefusedError, match="needs some text"):
        await do(store, host, OperatorAction.ANSWER, blocker_seq=asked.blocker_seq, text=" \n")

    host.end_mark = DONE
    await do(store, host, OperatorAction.ANSWER, blocker_seq=asked.blocker_seq, text="Return\n1.")
    assert host.prompts[-1] == (AgentKind.CLAUDE, "Return 1.")
    assert await runner.run(TASK) is TaskStatus.COMPLETED


async def test_a_stale_button_or_a_task_that_waits_for_nothing_is_refused() -> None:
    store, host, _, runner = await setup(end_mark=QUESTION)
    with pytest.raises(ActionRefusedError, match="not waiting"):
        await do(store, host, OperatorAction.ANSWER, blocker_seq=None, text="yes")

    assert await runner.run(TASK) is TaskStatus.NEEDS_HUMAN
    with pytest.raises(ActionRefusedError, match="moved on"):
        await do(store, host, OperatorAction.ANSWER, blocker_seq=1, text="yes")
    assert len(host.prompts) == 1  # nothing was typed


async def test_the_pane_is_verified_before_anything_is_sent() -> None:
    store, host, _, runner = await setup(end_mark=QUESTION)
    assert await runner.run(TASK) is TaskStatus.NEEDS_HUMAN
    for pane_id in list(host.screens):
        await host.close_pane(pane_id)

    with pytest.raises(ActionRefusedError, match="no longer hosts"):
        await do(store, host, OperatorAction.ANSWER, blocker_seq=None, text="yes")
    assert len(host.prompts) == 1


CHAT, OWNER = 1000, 7


def operator_bot(
    store: InMemoryEventStore, host: SimulatedAgents, woken: list[bool]
) -> OperatorBot:
    return OperatorBot(
        store=store,
        host=host,
        adapters=ADAPTERS,
        clock=CLOCK,
        settings=BotSettings(
            chat_id=CHAT,
            owner_id=OWNER,
            agents=(AgentKind.CLAUDE, AgentKind.CODEX),
            limit_cooldown=timedelta(hours=1),
        ),
        on_action=lambda: woken.append(True),
    )


async def test_only_the_owner_in_the_configured_chat_is_heard() -> None:
    store, host, _, _ = await setup()
    bot = operator_bot(store, host, [])
    assert bot.authorized(CHAT, OWNER)
    assert not bot.authorized(CHAT, 8)  # someone else in the chat
    assert not bot.authorized(2000, OWNER)  # the owner, elsewhere
    assert not bot.authorized(CHAT, None)


async def test_status_lists_open_tasks_with_their_blocker_and_the_agents() -> None:
    store, host, _, runner = await setup(end_mark=QUESTION)
    bot = operator_bot(store, host, [])
    assert await bot.command("/help") == HELP
    assert await runner.run(TASK) is TaskStatus.NEEDS_HUMAN

    status = await bot.command("/status@baton_bot")
    first, *_ = status.splitlines()
    assert first.startswith(f"{TASK}  needs_human  claude  power function  (#")
    assert first.endswith("blocked_question)")
    assert status.endswith("claude: available\ncodex: available")


async def test_a_reply_answers_the_question_and_wakes_the_daemon() -> None:
    store, host, notifier, runner = await setup(end_mark=QUESTION)
    woken: list[bool] = []
    bot = operator_bot(store, host, woken)
    assert await runner.run(TASK) is TaskStatus.NEEDS_HUMAN
    (asked,) = [n for n in notifier.notices if n.kind is NoticeKind.NEEDS_HUMAN]

    assert await bot.reply("[power] something else", "yes") is None
    host.end_mark = DONE
    assert await bot.reply(format_notice(asked), "Return 1.") == "Answer sent."
    assert woken == [True]
    assert host.prompts[-1] == (AgentKind.CLAUDE, "Return 1.")
    # The same reply again is refused: the question was answered.
    assert (
        await bot.reply(format_notice(asked), "Return 1.") == f"{TASK}: this was already answered."
    )
    assert await runner.run(TASK) is TaskStatus.COMPLETED


async def test_buttons_carry_their_blocker() -> None:
    store, host, notifier, runner = await setup(permission_after_prompt=True)
    bot = operator_bot(store, host, [])
    assert await runner.run(TASK) is TaskStatus.NEEDS_HUMAN
    (asked,) = [n for n in notifier.notices if n.kind is NoticeKind.NEEDS_HUMAN]
    assert asked.blocker_seq is not None

    assert await bot.button("approve:power") == "Unknown button."
    assert "moved on" in await bot.button(button_data(OperatorAction.APPROVE, TASK, 1))
    assert await bot.button(button_data(OperatorAction.APPROVE, TASK, asked.blocker_seq)) == (
        "Approved."
    )
    assert await runner.run(TASK) is TaskStatus.COMPLETED


async def test_a_denied_permission_waits_for_what_to_do_instead() -> None:
    store, host, notifier, runner = await setup(permission_after_prompt=True)
    assert await runner.run(TASK) is TaskStatus.NEEDS_HUMAN
    (asked,) = [n for n in notifier.notices if n.kind is NoticeKind.NEEDS_HUMAN]

    await do(store, host, OperatorAction.DENY, blocker_seq=asked.blocker_seq)
    # The agent stops without a mark, as Claude does after Esc: not a finished task.
    assert await runner.run(TASK) is TaskStatus.NEEDS_HUMAN
    after = [n for n in notifier.notices if n.kind is NoticeKind.NEEDS_HUMAN][-1]
    assert after.actions == (OperatorAction.ANSWER,)
    assert "denied" in after.text

    await do(store, host, OperatorAction.ANSWER, blocker_seq=after.blocker_seq, text="Skip it.")
    assert await runner.run(TASK) is TaskStatus.COMPLETED
