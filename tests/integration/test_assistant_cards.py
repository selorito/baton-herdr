"""Work described in words, started from a card only by the operator's press (ADR 0021):
a new project (phases A3 and A4), or a task in an existing directory."""

from __future__ import annotations

import asyncio
import json
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Any

import pytest

from baton_herdr.adapters import ADAPTERS
from baton_herdr.assistant.cards import CardStore
from baton_herdr.assistant.conversation import Assistant, AssistantConfig
from baton_herdr.assistant.places import Places
from baton_herdr.core.commands import complete_task
from baton_herdr.core.fakes import (
    FakeAssistant,
    FakeDirectories,
    FixedClock,
    InMemoryEventStore,
)
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


CONFIG = AssistantConfig(
    timeout_s=5, min_confidence=0.5, history_messages=4, language="auto", zone=UTC
)
TRUSTED = "/home/me/dev/baton-projects"
SMOKE = f"{TRUSTED}/wsl1-smoke"
DIVIDE = "calc.py'ye bölme işlemi ekle; sıfıra bölmede hata versin"


def in_existing(path: str, request: str = DIVIDE, **extra: Any) -> str:
    return json.dumps(
        {
            "intent": "new_project",
            "reply": "",
            "confidence": 0.9,
            "new_project": {
                "request": request,
                "target": {"kind": "existing", "path": path},
                **extra,
            },
        }
    )


class Setup:
    def __init__(
        self, root: Path, *answers: str, places: Places | None = None, task_tier: str = "premium"
    ) -> None:
        self.clock = FixedClock(NOW)
        self.store = InMemoryEventStore()
        self.model = FakeAssistant(*answers)
        self.directories = FakeDirectories(SMOKE, "/etc")
        if places is None:
            places = Places(directories=self.directories, trusted=(TRUSTED,))
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
                task_tier=task_tier,
            ),
            projects_root=root,
            assistant=Assistant(
                backend=self.model,
                store=self.store,
                config=CONFIG,
                cards=CardStore(clock=self.clock, ttl=timedelta(minutes=30)),
                places=places,
            ),
        )

    async def projects(self) -> list[str]:
        tasks = project(await self.store.read()).tasks.values()
        return [t.project for t in tasks if t.role == "plan" and t.project]

    async def tasks(self) -> list[Any]:
        return list(project(await self.store.read()).tasks.values())


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


async def test_work_in_a_trusted_directory_comes_as_a_card_and_starts_nothing(
    tmp_path: Path,
) -> None:
    setup = Setup(tmp_path, in_existing("~/dev/baton-projects/wsl1-smoke"))
    reply = await setup.bot.converse("~/dev/baton-projects/wsl1-smoke içinde calc.py'ye bölme ekle")
    assert reply.text.startswith(
        "🛠 İş önerisi\n"
        "Başlık: calc.py'ye bölme işlemi ekle\n"
        f"{DIVIDE}\n\n"
        f"Nerede: {SMOKE} (var olan klasör; baton klasör açmaz, taşımaz, silmez)\n"
        "Neden izinli: policy.yaml'daki trusted_dirs içinde\n"
    )
    assert "Ajan (şu an): claude: first in preference order" in reply.text
    assert "Katman: premium" in reply.text
    assert "plan aşaması yok" in reply.text
    assert [label for label, _ in reply.buttons] == ["Başlat", "Düzenle", "İptal"]
    assert await setup.tasks() == []


async def test_start_adds_one_task_in_that_directory_the_way_task_add_does(
    tmp_path: Path,
) -> None:
    setup = Setup(tmp_path, in_existing(SMOKE, f"{DIVIDE} #economy"), task_tier="premium")
    reply = await setup.bot.converse(f"{SMOKE} içinde calc.py'ye bölme ekle, ucuz modelle")
    assert "Katman: economy" in reply.text
    start = data(reply.buttons, "start")
    first = await setup.bot.button(start)
    (task,) = await setup.tasks()
    assert first == f"Task {task.task_id} in {SMOKE}: calc.py'ye bölme işlemi ekle."
    assert (task.workdir, task.instructions, task.tier) == (SMOKE, DIVIDE, Tier.ECONOMY)
    assert (task.project, task.role) == (None, None)
    assert await setup.bot.button(start) == f"Zaten başlatıldı. {first}"
    assert len(await setup.tasks()) == 1


async def test_a_project_baton_knows_is_offered_by_its_id(tmp_path: Path) -> None:
    setup = Setup(tmp_path, proposal(), in_existing("p-000000"))
    made = await setup.bot.button(
        data((await setup.bot.converse("make a site for my cafe")).buttons, "start")
    )
    project_id = made.split()[1]
    (plan,) = await setup.tasks()
    setup.directories.existing.add(plan.workdir)
    setup.model.answers = [in_existing(project_id, "Add the opening hours to the page")]
    reply = await setup.bot.converse(f"add the opening hours in {project_id}")
    assert f"Where: {plan.workdir} (an existing directory;" in reply.text
    assert f"Allowed because: it is baton's project {project_id}" in reply.text
    assert (await setup.bot.button(data(reply.buttons, "start"))).startswith("Task t-")


@pytest.mark.parametrize(
    ("path", "said"),
    [
        ("/etc", "/etc: not in the policy's trusted_dirs, and not a project baton made;"),
        (f"{TRUSTED}/calc", f"{TRUSTED}/calc: no such directory. baton makes, moves and"),
        ("calc", "calc: write the full path (/… or ~/…)"),
        ("p-ffffff", "p-ffffff: no such project in the log."),
    ],
)
async def test_any_other_directory_is_refused_with_the_reason(
    tmp_path: Path, path: str, said: str
) -> None:
    setup = Setup(tmp_path, in_existing(path, "Add a power function to calc"))
    reply = await setup.bot.converse("add a power function to calc")
    assert reply.buttons == ()
    assert reply.text.startswith(said)
    assert await setup.tasks() == []


async def test_the_directory_is_checked_again_when_start_is_pressed(tmp_path: Path) -> None:
    setup = Setup(tmp_path, in_existing(SMOKE, "Add division to calc.py"))
    reply = await setup.bot.converse("add division to calc.py in the smoke dir")
    setup.directories.existing.discard(SMOKE)  # removed while the card waited
    said = await setup.bot.button(data(reply.buttons, "start"))
    assert said.startswith(f"{SMOKE}: no such directory.")
    assert await setup.bot.button(data(reply.buttons, "start")) == said
    assert await setup.tasks() == []


async def test_edit_keeps_the_directory_in_front_of_the_model(tmp_path: Path) -> None:
    setup = Setup(
        tmp_path,
        in_existing(SMOKE, "Add division to calc.py"),
        in_existing(SMOKE, "Add division and modulo to calc.py"),
    )
    old = (await setup.bot.converse("add division to calc.py in the smoke dir")).buttons
    await setup.bot.button(data(old, "edit"))
    new = await setup.bot.converse("modulo too")
    assert f"Directory: {SMOKE}\n" in setup.model.turns[1].message
    assert "Add division and modulo to calc.py" in new.text
    assert await setup.bot.button(data(old, "start")) == "A newer card replaced this one."


async def test_without_places_existing_directories_are_not_offered(tmp_path: Path) -> None:
    setup = Setup(tmp_path)
    setup.bot.assistant = Assistant(
        backend=FakeAssistant(in_existing(SMOKE)),
        store=setup.store,
        config=CONFIG,
        cards=CardStore(clock=setup.clock, ttl=timedelta(minutes=30)),
    )
    reply = await setup.bot.converse("add division to calc.py in the smoke dir")
    assert reply.buttons == ()
    assert "baton task add" in reply.text


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
