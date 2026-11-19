"""A task cancelled or moved to another tier in words, done only on the operator's press of
a card that names the task (ADR 0021)."""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Any

from baton_herdr.adapters import ADAPTERS
from baton_herdr.assistant.cards import CardStore
from baton_herdr.assistant.conversation import Assistant, AssistantConfig
from baton_herdr.core.events import TaskCancelled, TaskCreated
from baton_herdr.core.fakes import FakeAssistant, FixedClock, InMemoryEventStore
from baton_herdr.core.model import AgentKind, TaskId, TaskStatus
from baton_herdr.core.projection import project
from baton_herdr.core.routing import Tier
from baton_herdr.telegram.bot import BotSettings, OperatorBot

from simulated_agents import SimulatedAgents

if TYPE_CHECKING:
    from pathlib import Path

NOW = datetime(2026, 11, 10, 9, 0, tzinfo=UTC)
MENU = TaskId("t-1a2b3c4d")
PRICES = TaskId("t-5e6f7a8b")


def cancel(**task: Any) -> str:
    return json.dumps(
        {"intent": "cancel", "reply": "", "confidence": 0.9, "cancel": {"task": task}}
    )


def set_tier(tier: str, **task: Any) -> str:
    return json.dumps(
        {
            "intent": "set_tier",
            "reply": "",
            "confidence": 0.9,
            "set_tier": {"task": task, "tier": tier},
        }
    )


class Setup:
    def __init__(self, root: Path, *answers: str, cards: bool = True) -> None:
        self.clock = FixedClock(NOW)
        self.store = InMemoryEventStore()
        self.model = FakeAssistant(*answers)
        self.bot = OperatorBot(
            store=self.store,
            host=SimulatedAgents(self.clock),
            adapters=ADAPTERS,
            clock=self.clock,
            settings=BotSettings(
                chat_id=1,
                owner_id=7,
                agents=(AgentKind.CLAUDE, AgentKind.CODEX),
                limit_cooldown=timedelta(hours=1),
            ),
            projects_root=root,
            assistant=Assistant(
                backend=self.model,
                store=self.store,
                config=AssistantConfig(
                    timeout_s=5, min_confidence=0.5, history_messages=4, language="auto", zone=UTC
                ),
                cards=CardStore(clock=self.clock, ttl=timedelta(minutes=30)) if cards else None,
            ),
        )

    async def add(self, task_id: TaskId, title: str) -> None:
        await self.store.append(
            [
                TaskCreated(
                    occurred_at=NOW, task_id=task_id, title=title, instructions="-", workdir="/w"
                )
            ]
        )

    async def status(self, task_id: str) -> TaskStatus:
        return project(await self.store.read()).tasks[TaskId(task_id)].status


def data(buttons: tuple[tuple[str, str], ...], action: str) -> str:
    return next(d for _, d in buttons if d.startswith(f"as:{action}:"))


async def test_a_cancel_by_id_comes_as_a_card_and_is_done_only_on_the_press(
    tmp_path: Path,
) -> None:
    setup = Setup(tmp_path, cancel(id=MENU))
    await setup.add(MENU, "Cafe menu page")
    reply = await setup.bot.converse(f"{MENU}'yi iptal et")
    assert reply.text.startswith(f'🛑 {MENU} "Cafe menu page" (pending) iptal edilsin mi?')
    assert "yeniden denenmez" in reply.text
    assert [label for label, _ in reply.buttons] == ["İptal et", "Vazgeç"]
    assert all(len(d.encode()) <= 64 for _, d in reply.buttons)
    assert await setup.status(MENU) is TaskStatus.PENDING
    start = data(reply.buttons, "start")
    assert await setup.bot.button(start) == f"Task cancelled. {MENU}."
    assert await setup.status(MENU) is TaskStatus.CANCELLED
    assert await setup.bot.button(start) == f"Zaten yapıldı. Task cancelled. {MENU}."
    cancelled = [e.event for e in await setup.store.read() if isinstance(e.event, TaskCancelled)]
    assert [e.reason for e in cancelled] == ["cancelled by telegram:7"]  # as /cancel does


async def test_a_task_named_in_words_is_found_by_baton(tmp_path: Path) -> None:
    setup = Setup(tmp_path, cancel(words="cafe"))
    await setup.add(MENU, "Cafe menu page")
    await setup.add(PRICES, "Bakery price list")
    reply = await setup.bot.converse("cancel the cafe task")
    assert reply.text.startswith(f'🛑 Cancel {MENU} "Cafe menu page" (pending)?')
    assert [label for label, _ in reply.buttons] == ["Cancel it", "Keep it"]
    assert await setup.bot.button(data(reply.buttons, "start")) == f"Task cancelled. {MENU}."
    assert await setup.status(PRICES) is TaskStatus.PENDING


async def test_a_tier_in_words_comes_as_a_card_and_is_set_as_tier_does(tmp_path: Path) -> None:
    setup = Setup(tmp_path, set_tier("economy", id=MENU))
    await setup.add(MENU, "Cafe menu page")
    reply = await setup.bot.converse(f"{MENU} ucuz modelde çalışsın")
    assert reply.text.startswith(
        f'🔀 {MENU} "Cafe menu page" (pending) economy (ucuz model) çalışsın mı?'
    )
    assert "Bir sonraki denemesinden geçerli." in reply.text
    assert [label for label, _ in reply.buttons] == ["Değiştir", "Vazgeç"]
    assert project(await setup.store.read()).tasks[MENU].tier is None
    assert await setup.bot.button(data(reply.buttons, "start")) == f"{MENU} runs economy."
    assert project(await setup.store.read()).tasks[MENU].tier is Tier.ECONOMY


async def test_keep_it_changes_nothing(tmp_path: Path) -> None:
    setup = Setup(tmp_path, set_tier("economy", id=MENU))
    await setup.add(MENU, "Cafe menu page")
    reply = await setup.bot.converse(f"run {MENU} on economy")
    assert await setup.bot.button(data(reply.buttons, "cancel")) == (
        "Left as it was; nothing changed."
    )
    assert await setup.bot.button(data(reply.buttons, "start")) == (
        "This card was dismissed; nothing changed."
    )
    assert project(await setup.store.read()).tasks[MENU].tier is None
    edit = data(reply.buttons, "start").replace("as:start:", "as:edit:")
    assert await setup.bot.button(edit) == "This card was dismissed; nothing changed."


async def test_an_action_card_has_no_edit(tmp_path: Path) -> None:
    setup = Setup(tmp_path, cancel(id=MENU))
    await setup.add(MENU, "Cafe menu page")
    reply = await setup.bot.converse(f"cancel {MENU}")
    edit = data(reply.buttons, "start").replace("as:start:", "as:edit:")
    assert await setup.bot.button(edit) == "This card has no such button."
    assert await setup.bot.button(data(reply.buttons, "start")) == f"Task cancelled. {MENU}."


async def test_words_that_fit_two_tasks_get_a_question_and_the_id_answers_it(
    tmp_path: Path,
) -> None:
    # One answer only: the reply to the question is read without the model.
    setup = Setup(tmp_path, cancel(words="cafe"))
    await setup.add(MENU, "Cafe menu page")
    await setup.add(PRICES, "Cafe price list")
    question = await setup.bot.converse("cancel the cafe task")
    assert question.buttons == ()
    assert question.text == (
        "Which one? Write its id:\n"
        f'- {MENU} "Cafe menu page" (pending)\n'
        f'- {PRICES} "Cafe price list" (pending)'
    )
    card = await setup.bot.converse(PRICES)
    assert card.text.startswith(f'🛑 Cancel {PRICES} "Cafe price list" (pending)?')
    assert len(setup.model.turns) == 1
    assert await setup.bot.button(data(card.buttons, "start")) == f"Task cancelled. {PRICES}."
    assert await setup.status(MENU) is TaskStatus.PENDING


async def test_an_unknown_task_gets_a_question_not_a_guess(tmp_path: Path) -> None:
    setup = Setup(
        tmp_path,
        cancel(words="bakery"),
        cancel(id="t-99999999"),
        cancel(id=MENU),  # an id the operator did not write
    )
    await setup.add(MENU, "Cafe menu page")
    words = await setup.bot.converse("cancel the bakery task")
    assert (words.text, words.buttons) == (
        "No open task fits 'bakery'. Write its id; /status lists them.",
        (),
    )
    unknown = await setup.bot.converse("cancel t-99999999")
    assert (unknown.text, unknown.buttons) == ("t-99999999: no such task in the log.", ())
    made_up = await setup.bot.converse("cancel that one")
    assert (made_up.text, made_up.buttons) == (
        "Which task? Write its id (t-…); /status lists them.",
        (),
    )
    assert await setup.status(MENU) is TaskStatus.PENDING


async def test_a_closed_task_gets_no_card(tmp_path: Path) -> None:
    setup = Setup(tmp_path, cancel(id=MENU), set_tier("premium", id=MENU))
    await setup.add(MENU, "Cafe menu page")
    await setup.bot.cancel(MENU)
    for message in (f"cancel {MENU}", f"run {MENU} on premium"):
        reply = await setup.bot.converse(message)
        assert (reply.text, reply.buttons) == (
            f"{MENU} is already cancelled; nothing to change.",
            (),
        )


async def test_a_task_closed_after_the_card_is_refused_by_the_command(tmp_path: Path) -> None:
    setup = Setup(tmp_path, set_tier("economy", id=MENU))
    await setup.add(MENU, "Cafe menu page")
    reply = await setup.bot.converse(f"run {MENU} on economy")
    await setup.bot.cancel(MENU)
    assert await setup.bot.button(data(reply.buttons, "start")) == (f"{MENU} is already cancelled.")


async def test_a_project_is_cancelled_whole_from_its_card(tmp_path: Path) -> None:
    setup = Setup(tmp_path)
    made = await setup.bot.new_project("A one-page website for my cafe")
    project_id = made.split()[1]
    setup.model.answers.append(cancel(id=project_id))
    reply = await setup.bot.converse(f"{project_id} projesini iptal et")
    assert reply.text.startswith(f"🛑 {project_id} projesi iptal edilsin mi?\nAçık 1 işi")
    assert '"Plan: A one-page website for my cafe"' in reply.text
    assert await setup.bot.button(data(reply.buttons, "start")) == (
        f"Project {project_id} cancelled: 1 task(s) dropped."
    )


async def test_without_cards_the_assistant_names_the_command(tmp_path: Path) -> None:
    setup = Setup(tmp_path, set_tier("economy", words="menu"), cards=False)
    await setup.add(MENU, "Cafe menu page")
    reply = await setup.bot.converse("run the menu task on economy")
    assert (reply.text, reply.buttons) == (f"/tier {MENU} economy", ())
    assert project(await setup.store.read()).tasks[MENU].tier is None
