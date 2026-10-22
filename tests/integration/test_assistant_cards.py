"""A project described in words, started from a card only by the operator's press
(ADR 0021, phases A3 and A4)."""

from __future__ import annotations

import asyncio
import json
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Any

from baton_herdr.adapters import ADAPTERS
from baton_herdr.assistant.cards import CardStore
from baton_herdr.assistant.conversation import Assistant, AssistantConfig
from baton_herdr.core.commands import complete_task
from baton_herdr.core.fakes import FakeAssistant, FixedClock, InMemoryEventStore
from baton_herdr.core.model import AgentKind
from baton_herdr.core.projection import project
from baton_herdr.core.routing import Tier
from baton_herdr.projects.flow import propose_plans
from baton_herdr.telegram.bot import BotSettings, OperatorBot

from simulated_agents import SimulatedAgents

if TYPE_CHECKING:
    from pathlib import Path

NOW = datetime(2026, 10, 23, 9, 0, tzinfo=UTC)
REQUEST = "A one-page website for my cafe, with the menu"
PLAN = {"summary": "A page.", "steps": [{"title": "Page", "instructions": "Write index.html."}]}


def proposal(request: str = REQUEST, **extra: Any) -> str:
    return json.dumps(
        {
            "intent": "new_project",
            "reply": "",
            "confidence": 0.9,
            "new_project": {"request": request, "target": {"kind": "new"}, **extra},
        }
    )


class Setup:
    def __init__(self, root: Path, *answers: str) -> None:
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
                cards=CardStore(clock=self.clock, ttl=timedelta(minutes=30)),
            ),
        )

    async def projects(self) -> list[str]:
        tasks = project(await self.store.read()).tasks.values()
        return [t.project for t in tasks if t.role == "plan" and t.project]


def data(buttons: tuple[tuple[str, str], ...], action: str) -> str:
    return next(d for _, d in buttons if d.startswith(f"as:{action}:"))


async def test_a_proposal_comes_as_a_card_and_starts_nothing(tmp_path: Path) -> None:
    setup = Setup(tmp_path, proposal())
    reply = await setup.bot.converse("kafem için menülü bir site yap")
    assert reply.text.startswith(
        f"📝 Proje önerisi\n{REQUEST}\n\nNerede: {tmp_path}/… (klasörü baton açar)"
    )
    assert "Ajan (şu an): claude: first in preference order" in reply.text
    assert "Katman: premium" in reply.text
    assert "onaylamadan kod yazılmaz" in reply.text
    assert "Kart 30 dakika geçerli." in reply.text
    assert [label for label, _ in reply.buttons] == ["Başlat", "Düzenle", "İptal"]
    assert all(len(d.encode()) <= 64 for _, d in reply.buttons)  # Telegram's limit
    assert await setup.projects() == []


async def test_start_makes_the_project_the_way_new_does_and_only_once(tmp_path: Path) -> None:
    setup = Setup(tmp_path, proposal())
    reply = await setup.bot.converse("make a site for my cafe")
    start = data(reply.buttons, "start")
    first = await setup.bot.button(start)
    assert first.startswith("Project p-")
    again = await setup.bot.button(start)
    assert again == f"Already started. {first}"
    (made,) = await setup.projects()
    plan = next(t for t in project(await setup.store.read()).tasks.values() if t.role == "plan")
    assert (plan.request, plan.project) == (REQUEST, made)


async def test_two_quick_presses_start_one_project(tmp_path: Path) -> None:
    setup = Setup(tmp_path, proposal())
    start = data((await setup.bot.converse("make a site for my cafe")).buttons, "start")
    results = await asyncio.gather(setup.bot.button(start), setup.bot.button(start))
    assert sum(r.startswith("Project p-") for r in results) == 1
    assert len(await setup.projects()) == 1


async def test_a_cancelled_or_expired_card_starts_nothing(tmp_path: Path) -> None:
    setup = Setup(tmp_path, proposal(), proposal())
    cancelled = (await setup.bot.converse("make a site for my cafe")).buttons
    assert await setup.bot.button(data(cancelled, "cancel")) == "Cancelled; nothing was started."
    assert await setup.bot.button(data(cancelled, "start")) == "This card was cancelled."
    late = (await setup.bot.converse("make a site for my cafe")).buttons
    setup.clock.advance(timedelta(minutes=31))
    assert await setup.bot.button(data(late, "start")) == (
        "This card has expired; send the request again."
    )
    assert await setup.projects() == []


async def test_a_card_from_before_a_restart_has_expired(tmp_path: Path) -> None:
    setup = Setup(tmp_path)
    assert await setup.bot.button("as:start:0a1b2c3d") == (
        "This card has expired; send the request again."
    )


async def test_edit_takes_the_next_message_as_the_change_and_replaces_the_card(
    tmp_path: Path,
) -> None:
    changed = "A one-page website for my cafe, with the menu and opening hours"
    setup = Setup(tmp_path, proposal(), proposal(changed))
    old = (await setup.bot.converse("make a site for my cafe")).buttons
    assert await setup.bot.button(data(old, "edit")) == (
        "What should change? I will take your next message as the change."
    )
    new = await setup.bot.converse("add the opening hours")
    assert changed in new.text
    edit_turn = setup.model.turns[1]
    assert f"Request: {REQUEST}" in edit_turn.message
    assert "Change: add the opening hours" in edit_turn.message
    assert await setup.bot.button(data(old, "start")) == "A newer card replaced this one."
    assert (await setup.bot.button(data(new.buttons, "start"))).startswith("Project p-")


async def test_a_request_missing_something_gets_a_question_not_a_card(tmp_path: Path) -> None:
    setup = Setup(tmp_path, proposal(question="Which pages should it have?"))
    reply = await setup.bot.converse("bana bir site yap")
    assert (reply.text, reply.buttons) == ("Which pages should it have?", ())


async def test_work_in_an_existing_directory_is_not_offered_yet(tmp_path: Path) -> None:
    existing = json.dumps(
        {
            "intent": "new_project",
            "reply": "",
            "confidence": 0.9,
            "new_project": {
                "request": "Add a power function to calc",
                "target": {"kind": "existing", "path": "/home/me/src/calc"},
            },
        }
    )
    setup = Setup(tmp_path, existing)
    reply = await setup.bot.converse("add a power function to my calc repo")
    assert reply.buttons == ()
    assert "baton task add" in reply.text
    assert await setup.projects() == []


async def test_a_tag_in_the_request_shows_on_the_card_and_sets_the_projects_tier(
    tmp_path: Path,
) -> None:
    setup = Setup(tmp_path, proposal(f"{REQUEST} #economy"))
    reply = await setup.bot.converse("make a cheap site for my cafe")
    assert "Tier: economy" in reply.text
    await setup.bot.button(data(reply.buttons, "start"))
    plan = next(t for t in project(await setup.store.read()).tasks.values() if t.role == "plan")
    assert plan.tier is Tier.ECONOMY


async def test_nothing_is_coded_before_the_plan_is_started(tmp_path: Path) -> None:
    """A4: the card starts the planning only; steps exist once the plan's own Start is
    pressed, as for /new (ADR 0016)."""
    setup = Setup(tmp_path, proposal())
    reply = await setup.bot.converse("make a site for my cafe")
    await setup.bot.button(data(reply.buttons, "start"))

    def steps(tasks: Any) -> list[str]:
        return [t.task_id for t in tasks if t.role == "step"]

    board = project(await setup.store.read())
    (plan,) = [t for t in board.tasks.values() if t.role == "plan"]
    assert steps(board.tasks.values()) == []
    workdir = tmp_path / plan.workdir.rsplit("/", 1)[-1]
    (workdir / ".baton" / "plan.json").write_text(json.dumps(PLAN), encoding="utf-8")
    await setup.store.append(complete_task(plan, at=NOW))
    await propose_plans(
        setup.store, clock=setup.clock, read_text=lambda p: p.read_text(encoding="utf-8")
    )
    assert steps(project(await setup.store.read()).tasks.values()) == []
    await setup.bot.button(f"plan:start:{plan.task_id}")
    assert len(steps(project(await setup.store.read()).tasks.values())) == 1
