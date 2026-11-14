"""Stop words stop the work under way at once, read by baton, never by the model
(ADR 0024)."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING

import pytest

from baton_herdr.adapters import ADAPTERS
from baton_herdr.assistant.conversation import Assistant, AssistantConfig
from baton_herdr.assistant.stop import StopRequest, read_stop
from baton_herdr.core.events import AttemptStarted, TaskCancelled, TaskCreated
from baton_herdr.core.fakes import FakeAssistant, FixedClock, InMemoryEventStore
from baton_herdr.core.model import AgentKind, TaskId, TaskStatus, new_attempt_id
from baton_herdr.core.projection import project
from baton_herdr.telegram.bot import BotSettings, OperatorBot

from simulated_agents import SimulatedAgents

if TYPE_CHECKING:
    from pathlib import Path

NOW = datetime(2026, 11, 14, 9, 0, tzinfo=UTC)
MENU = TaskId("t-1a2b3c4d")
PRICES = TaskId("t-5e6f7a8b")
QUEUED = TaskId("t-9c0d1e2f")


@pytest.mark.parametrize(
    ("message", "expected"),
    [
        ("dur", StopRequest("tr")),
        ("Dur!", StopRequest("tr")),
        ("DUR", StopRequest("tr")),
        ("durdur", StopRequest("tr")),
        ("hemen dur", StopRequest("tr")),
        ("iptal", StopRequest("tr")),
        ("İPTAL ET", StopRequest("tr")),
        ("vazgeç", StopRequest("tr")),
        ("yeter 🛑", StopRequest("tr")),
        ("hepsini durdur", StopRequest("tr", everything=True)),
        ("dur t-1a2b3c4d", StopRequest("tr", ids=("t-1a2b3c4d",))),
        ("stop", StopRequest("en")),
        ("Stop it now.", StopRequest("en")),
        ("cancel everything", StopRequest("en", everything=True)),
    ],
)
def test_a_stop_word_is_read_without_the_model(message: str, expected: StopRequest) -> None:
    assert read_stop(message) == expected


@pytest.mark.parametrize(
    "message",
    [
        "",
        "merhaba",
        "durum ne?",  # status, not a stop
        "kafe görevini durdur",  # names a task in words: the assistant's card
        "rengi kırmızı yap ve dur",
        "don't stop the build",
        "bus stop near the cafe",
        "iptal edilen görevler neden iptal oldu bana açıklar mısın lütfen",
        "t-1a2b3c4d",
    ],
)
def test_anything_else_is_not_a_stop(message: str) -> None:
    assert read_stop(message) is None


class Setup:
    def __init__(self, root: Path, *, assistant: bool = False) -> None:
        self.clock = FixedClock(NOW)
        self.store = InMemoryEventStore()
        self.model = FakeAssistant()
        self.bot = OperatorBot(
            store=self.store,
            host=SimulatedAgents(self.clock),
            adapters=ADAPTERS,
            clock=self.clock,
            settings=BotSettings(
                chat_id=1,
                owner_id=7,
                agents=(AgentKind.CLAUDE,),
                limit_cooldown=timedelta(hours=1),
            ),
            projects_root=root,
            assistant=Assistant(
                backend=self.model,
                store=self.store,
                config=AssistantConfig(
                    timeout_s=5, min_confidence=0.5, history_messages=4, language="auto", zone=UTC
                ),
            )
            if assistant
            else None,
        )

    async def add(self, task_id: TaskId, title: str, *, running: bool = True) -> None:
        events: list[TaskCreated | AttemptStarted] = [
            TaskCreated(
                occurred_at=NOW, task_id=task_id, title=title, instructions="-", workdir="/w"
            )
        ]
        if running:
            events.append(
                AttemptStarted(
                    occurred_at=NOW,
                    task_id=task_id,
                    attempt_id=new_attempt_id(),
                    agent=AgentKind.CLAUDE,
                )
            )
        await self.store.append(events)

    async def status(self, task_id: TaskId) -> TaskStatus:
        return project(await self.store.read()).tasks[task_id].status


async def test_the_one_task_running_is_cancelled_at_once(tmp_path: Path) -> None:
    setup = Setup(tmp_path, assistant=True)
    await setup.add(MENU, "Cafe menu page")
    await setup.add(QUEUED, "Waiting its turn", running=False)

    reply = await setup.bot.stop("dur")

    assert reply is not None
    assert reply.text == f"🛑 Durduruldu:\n• {MENU} Cafe menu page"
    assert reply.buttons == ()
    assert await setup.status(MENU) is TaskStatus.CANCELLED
    assert await setup.status(QUEUED) is TaskStatus.PENDING  # not under way: left alone
    assert setup.model.turns == []  # the model was never asked
    cancelled = [e.event for e in await setup.store.read() if isinstance(e.event, TaskCancelled)]
    assert [e.reason for e in cancelled] == ["cancelled by telegram:7"]  # as /cancel does


async def test_a_stop_works_without_the_assistant(tmp_path: Path) -> None:
    setup = Setup(tmp_path)
    await setup.add(MENU, "Cafe menu page")

    assert await setup.bot.command("stop") == f"🛑 Stopped:\n• {MENU} Cafe menu page"
    assert await setup.status(MENU) is TaskStatus.CANCELLED


async def test_nothing_running_says_so(tmp_path: Path) -> None:
    setup = Setup(tmp_path)
    await setup.add(QUEUED, "Waiting its turn", running=False)

    reply = await setup.bot.stop("dur")

    assert reply is not None
    assert reply.text == "Çalışan bir iş yok."
    assert await setup.status(QUEUED) is TaskStatus.PENDING


async def test_several_running_ask_which_with_buttons(tmp_path: Path) -> None:
    setup = Setup(tmp_path)
    await setup.add(MENU, "Cafe menu page")
    await setup.add(PRICES, "Price list")

    reply = await setup.bot.stop("dur")

    assert reply is not None
    assert reply.text == "2 iş çalışıyor. Hangisi durdurulsun?"
    assert [d for _, d in reply.buttons] == [
        f"task:cancel:{MENU}",
        f"task:cancel:{PRICES}",
        "stop:all:tr",
    ]
    assert all(len(d.encode()) <= 64 for _, d in reply.buttons)
    assert await setup.status(MENU) is TaskStatus.RUNNING
    assert await setup.bot.button(f"task:cancel:{PRICES}") == f"Task cancelled. {PRICES}."
    assert await setup.status(MENU) is TaskStatus.RUNNING
    assert await setup.status(PRICES) is TaskStatus.CANCELLED


async def test_stop_them_all_from_the_button(tmp_path: Path) -> None:
    setup = Setup(tmp_path)
    await setup.add(MENU, "Cafe menu page")
    await setup.add(PRICES, "Price list")

    said = await setup.bot.button("stop:all:tr")

    assert said.startswith("🛑 Durduruldu:")
    assert await setup.status(MENU) is TaskStatus.CANCELLED
    assert await setup.status(PRICES) is TaskStatus.CANCELLED
    assert await setup.bot.button("stop:all:tr") == "Çalışan bir iş yok."


async def test_hepsini_durdur_stops_everything_without_asking(tmp_path: Path) -> None:
    setup = Setup(tmp_path)
    await setup.add(MENU, "Cafe menu page")
    await setup.add(PRICES, "Price list")

    reply = await setup.bot.stop("hepsini durdur")

    assert reply is not None
    assert reply.buttons == ()
    assert await setup.status(MENU) is TaskStatus.CANCELLED
    assert await setup.status(PRICES) is TaskStatus.CANCELLED


async def test_an_id_with_the_stop_word_stops_that_one(tmp_path: Path) -> None:
    setup = Setup(tmp_path)
    await setup.add(MENU, "Cafe menu page")
    await setup.add(PRICES, "Price list")

    await setup.bot.stop(f"dur {PRICES}")

    assert await setup.status(MENU) is TaskStatus.RUNNING
    assert await setup.status(PRICES) is TaskStatus.CANCELLED


async def test_a_running_project_step_stops_its_whole_project(tmp_path: Path) -> None:
    setup = Setup(tmp_path)
    made = await setup.bot.new_project("A one-page website for my cafe")
    project_id = made.split()[1]
    board = project(await setup.store.read())
    (plan,) = [t for t in board.tasks.values() if t.project == project_id]
    await setup.store.append(
        [
            AttemptStarted(
                occurred_at=NOW,
                task_id=plan.task_id,
                attempt_id=new_attempt_id(),
                agent=AgentKind.CLAUDE,
            )
        ]
    )

    reply = await setup.bot.stop("dur")

    assert reply is not None
    assert reply.text == f"🛑 Durduruldu:\n• {project_id} A one-page website for my cafe"
    assert await setup.status(plan.task_id) is TaskStatus.CANCELLED


async def test_a_sentence_goes_to_the_assistant_not_the_stop(tmp_path: Path) -> None:
    setup = Setup(tmp_path, assistant=True)
    await setup.add(MENU, "Cafe menu page")

    assert await setup.bot.stop("kafe menüsü görevini durdur") is None
    assert await setup.status(MENU) is TaskStatus.RUNNING
