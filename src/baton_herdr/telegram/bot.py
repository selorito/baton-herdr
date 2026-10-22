"""The operator's side of the Telegram bot: status, buttons and answers (ADR 0009).

``OperatorBot`` holds the logic and knows nothing about aiogram, so it is tested
directly; ``run_bot`` connects it to Telegram with long polling.

Only updates from the configured chat *and* the configured owner are handled;
anything else is dropped without a reply. Every action goes through
``baton_herdr.scheduler.operator.act``, which checks that the blocker is still open and
that the pane hosts the attempt before anything is typed.
"""

from __future__ import annotations

import asyncio
import re
import time
from contextlib import asynccontextmanager, suppress
from dataclasses import dataclass
from typing import TYPE_CHECKING
from zoneinfo import ZoneInfo

from baton_herdr.budget.availability import fold_availability
from baton_herdr.budget.report import budget_lines, when
from baton_herdr.core.backoff import Backoff
from baton_herdr.core.logging import get_logger
from baton_herdr.core.model import OperatorAction, TaskId
from baton_herdr.core.projection import project
from baton_herdr.core.routing import Tier
from baton_herdr.projects.create import ProjectSetupError
from baton_herdr.projects.flow import (
    ProjectError,
    approve_plan,
    cancel_project,
    reject_plan,
    start_project,
)
from baton_herdr.scheduler.notices import tier_mark
from baton_herdr.scheduler.operator import (
    ActionRefusedError,
    act,
    cancel,
    find_blocker,
    set_tier,
)
from baton_herdr.telegram.notifier import parse_reference

if TYPE_CHECKING:
    from collections.abc import AsyncIterator, Awaitable, Callable, Mapping
    from datetime import timedelta
    from pathlib import Path

    from aiogram import Bot
    from aiogram.types import CallbackQuery, Message

    from baton_herdr.assistant.conversation import Assistant
    from baton_herdr.budget.load import BudgetReader
    from baton_herdr.core.agents import AgentAdapter
    from baton_herdr.core.model import AgentKind
    from baton_herdr.core.panes import PaneHost
    from baton_herdr.core.ports import Clock, EventStore

HELP = (
    "/new <request>: start a project, e.g. /new a one-page website for my cafe. "
    "An agent plans it; you get the plan with Start and Cancel. "
    "Add #economy to run it on the cheaper model, #premium on the usual one.\n"
    "/status: open tasks and agents.\n"
    "/cancel <task or project>: drop it and close its agent's pane, e.g. /cancel t-1a2b3c4d.\n"
    "/budget: what each agent has left.\n"
    "/tier <task> economy|premium: the tier it runs in from its next attempt.\n"
    "Approve or Deny under a permission prompt; reply to a question to answer it."
)
# A word of its own: "#economy", not "cafe#economy".
_TIER_TAG = re.compile(r"(?<!\S)#(economy|premium)(?!\S)", re.IGNORECASE)
PLAN_STARTED = "Started."
PLAN_CANCELLED = "Cancelled."
TASK_CANCELLED = "Task cancelled."
# Said to a message that is neither a command nor a reply: it reached no agent.
NOT_A_REPLY = "That message went to no agent. To answer a question, reply to its message."
_DONE = {
    OperatorAction.APPROVE: "Approved.",
    OperatorAction.DENY: "Denied.",
    OperatorAction.ANSWER: "Answer sent.",
}


@dataclass(frozen=True, slots=True)
class BotSettings:
    chat_id: int
    owner_id: int
    agents: tuple[AgentKind, ...]
    limit_cooldown: timedelta
    # Zone for the times shown, as configured for the scheduler.
    timezone: str = "UTC"


class OperatorBot:
    def __init__(  # noqa: PLR0913 - every collaborator is a separate port
        self,
        *,
        store: EventStore,
        host: PaneHost,
        adapters: Mapping[AgentKind, AgentAdapter],
        clock: Clock,
        settings: BotSettings,
        budget: BudgetReader | None = None,
        on_action: Callable[[], None] | None = None,
        projects_root: Path | None = None,
        assistant: Assistant | None = None,
    ) -> None:
        self._store = store
        self._host = host
        self._adapters = adapters
        self._clock = clock
        self._settings = settings
        self._budget = budget
        self._zone = ZoneInfo(settings.timezone)
        # Called after an action was carried out, so the daemon looks at the task now.
        self._on_action = on_action or (lambda: None)
        # Where /new makes projects (ADR 0016); None: /new is not offered.
        self._projects_root = projects_root
        # Plain messages, understood by a local model (ADR 0021); None: answered as before.
        self.assistant = assistant
        self._log = get_logger("baton.telegram")

    def authorized(self, chat_id: int | None, user_id: int | None) -> bool:
        allowed = chat_id == self._settings.chat_id and user_id == self._settings.owner_id
        if not allowed:
            self._log.warning("telegram update ignored", chat_id=chat_id, user_id=user_id)
        return allowed

    async def command(self, text: str) -> str:  # noqa: PLR0911 - one return per command
        name = text.split(maxsplit=1)[0].split("@", maxsplit=1)[0] if text.strip() else ""
        if name == "/status":
            return await self.status()
        if name == "/budget":
            return await self.budget()
        if name == "/cancel":
            return await self.cancel(
                text.split(maxsplit=1)[1].strip() if " " in text.strip() else ""
            )
        if name == "/new":
            return await self.new_project(text.split(maxsplit=1)[1] if " " in text.strip() else "")
        if name == "/tier":
            return await self.tier(text.split()[1:])
        if not name.startswith("/"):
            if self.assistant is not None and text.strip():
                return await self.assistant.answer(text.strip())
            return f"{NOT_A_REPLY}\n\n{HELP}"
        return HELP

    async def budget(self) -> str:
        if self._budget is None:
            return "Budgets are not available."
        now = self._clock.now()
        budgets = await self._budget.read(self._settings.agents, await self._store.read(), now)
        return "\n".join(budget_lines(budgets.values(), now=now, zone=self._zone))

    async def status(self) -> str:
        events = await self._store.read()
        board = project(events)
        lines = []
        for task in board.tasks.values():
            if task.status.is_terminal:
                continue
            agent = task.attempts[-1].agent.value if task.attempts else "-"
            if mark := tier_mark(task):
                agent = f"{agent} ({mark})"
            line = f"{task.task_id}  {task.status.value}  {agent}  {task.title}"
            blocker = find_blocker(events, task.task_id)
            if blocker is not None:
                line += f"  (#{blocker.seq} {blocker.state.value})"
            lines.append(line)
        availability = fold_availability(events, cooldown=self._settings.limit_cooldown)
        now = self._clock.now()
        agents = []
        for agent in self._settings.agents:
            until = availability.limited_until.get(agent)
            limited = until is not None and until > now
            agents.append(
                f"{agent.value}: limited until {when(until, now, self._zone)}"
                if limited and until is not None
                else f"{agent.value}: available"
            )
        return "\n".join([*(lines or ["No open tasks."]), "", *agents])

    async def new_project(self, request: str) -> str:
        """``/new <request>``: a project and its plan task (ADR 0016)."""
        if self._projects_root is None:
            return "Projects are not set up."
        request, tier = split_tier(request)
        try:
            project_id, task_id, workdir = await start_project(
                self._store,
                root=self._projects_root,
                request=request,
                clock=self._clock,
                tier=tier,
            )
        except (ProjectError, ProjectSetupError) as err:
            return str(err)
        self._on_action()
        return (
            f"Project {project_id} in {workdir}. An agent plans it now ({task_id}); "
            "the plan comes here for you to start or cancel."
        )

    async def tier(self, args: list[str]) -> str:
        """``/tier t-… economy|premium`` (ADR 0020)."""
        if len(args) != 2 or args[1] not in {t.value for t in Tier}:
            return "Say which task and tier: /tier t-1a2b3c4d economy, or premium."
        task_id, tier = TaskId(args[0]), Tier(args[1])
        try:
            running = await set_tier(
                store=self._store,
                clock=self._clock,
                task_id=task_id,
                tier=tier,
                by=f"telegram:{self._settings.owner_id}",
            )
        except ActionRefusedError as err:
            return str(err)
        self._on_action()
        later = " from its next attempt; the one under way runs on" if running else ""
        return f"{task_id} runs {tier.value}{later}."

    async def cancel(self, target: str) -> str:
        """``/cancel t-…`` drops a task, ``/cancel p-…`` a whole project."""
        by = f"telegram:{self._settings.owner_id}"
        try:
            if target.startswith("p-"):
                count = await cancel_project(
                    self._store, self._host, project_id=target, by=by, clock=self._clock
                )
                result = f"Project {target} cancelled: {count} task(s) dropped."
            elif target:
                closed = await cancel(
                    store=self._store,
                    host=self._host,
                    clock=self._clock,
                    task_id=TaskId(target),
                    by=by,
                )
                result = f"{TASK_CANCELLED} {target}" + ("; its pane is closed." if closed else ".")
            else:
                return "Say what to cancel: /cancel t-1a2b3c4d, or /cancel p-1a2b3c for a project."
        except (ActionRefusedError, ProjectError) as err:
            return str(err)
        self._on_action()
        return result

    async def plan_choice(self, data: str) -> str:
        """A press on a plan's Start or Cancel; ``data`` is ``plan:start:t-1a2b3c4d``."""
        _, choice, task_id = [*data.split(":"), "", ""][:3]
        by = f"telegram:{self._settings.owner_id}"
        try:
            if choice == "start":
                steps = await approve_plan(self._store, TaskId(task_id), by=by, clock=self._clock)
            elif choice == "cancel":
                await reject_plan(self._store, TaskId(task_id), by=by, clock=self._clock)
                return PLAN_CANCELLED
            else:
                return "Unknown button."
        except ProjectError as err:
            return str(err)
        self._on_action()
        board = project(await self._store.read())
        first = [s for s in steps if board.tasks[s].branch is not None and not board.tasks[s].needs]
        if first:  # a parallel plan (ADR 0017)
            return f"{PLAN_STARTED} {len(steps)} steps, {len(first)} of them at once to begin with."
        return f"{PLAN_STARTED} {len(steps)} steps, one after another."

    async def button(self, data: str) -> str:
        """A press on Approve / Deny (``action:task:seq``) or on a plan's Start / Cancel."""
        if data.startswith("plan:"):
            return await self.plan_choice(data)
        if data.startswith("task:cancel:"):
            return await self.cancel(data.removeprefix("task:cancel:"))
        try:
            action_name, task_id, seq = data.split(":")
            action = OperatorAction(action_name)
            blocker_seq = int(seq)
        except ValueError:
            return "Unknown button."
        if action is OperatorAction.ANSWER:
            return "Reply to the message to answer."
        return await self._act(TaskId(task_id), action, blocker_seq)

    async def reply(self, replied_text: str, text: str) -> str | None:
        """A reply to one of the bot's notices; ``None`` if it was not a question."""
        reference = parse_reference(replied_text)
        if reference is None:
            return None
        task_id, blocker_seq = reference
        return await self._act(TaskId(task_id), OperatorAction.ANSWER, blocker_seq, text)

    async def _act(
        self, task_id: TaskId, action: OperatorAction, blocker_seq: int, text: str = ""
    ) -> str:
        try:
            await act(
                store=self._store,
                host=self._host,
                adapters=self._adapters,
                clock=self._clock,
                task_id=task_id,
                action=action,
                by=f"telegram:{self._settings.owner_id}",
                blocker_seq=blocker_seq,
                text=text,
            )
        except ActionRefusedError as err:
            return str(err)
        self._on_action()
        return _DONE[action]


def split_tier(request: str) -> tuple[str, Tier | None]:
    """``#economy`` or ``#premium`` in a ``/new`` request: the project's tier, taken out of
    the request (ADR 0020); the last one written wins. Without one the request is left as
    it is."""
    tags = _TIER_TAG.findall(request)
    if not tags:
        return request, None
    return _TIER_TAG.sub("", request).strip(), Tier(tags[-1].lower())


async def keep_running(
    start: Callable[[], Awaitable[None]],
    *,
    backoff: Backoff | None = None,
    sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    clock: Callable[[], float] = time.monotonic,
) -> None:
    """Run ``start`` (the bot) again whenever it stops with an error, until cancelled.

    Telegram may be out of reach when batond starts (the network comes up after it at
    boot) or later; without this, approvals and replies stopped until batond restarted,
    while notices still went out and nobody noticed. Waits longer after each quick failure.
    """
    backoff = backoff or Backoff()
    log = get_logger("baton.telegram")
    delay = backoff.first_s
    while True:
        started = clock()
        try:
            await start()
        except asyncio.CancelledError:
            raise
        except Exception as err:  # noqa: BLE001 - any failure: try again later, never stop batond
            if clock() - started >= backoff.healthy_s:
                delay = backoff.first_s
            log.warning("telegram bot stopped; starting it again", error=repr(err), after_s=delay)
            await sleep(delay)
            delay = min(delay * 2, backoff.max_s)
        else:
            return  # polling ended on its own: batond is stopping


@asynccontextmanager
async def _typing(bot: Bot, chat_id: int) -> AsyncIterator[None]:
    """Show "typing…" while the assistant thinks: Telegram clears it after 5 s."""

    async def keep() -> None:
        while True:
            with suppress(Exception):  # a cosmetic signal; the answer still goes out
                await bot.send_chat_action(chat_id, "typing")
            await asyncio.sleep(4)

    task = asyncio.create_task(keep())
    try:
        yield
    finally:
        task.cancel()
        with suppress(asyncio.CancelledError):
            await task


async def run_bot(bot: Bot, operator: OperatorBot) -> None:
    """Long-poll Telegram and hand authorised updates to ``operator`` until cancelled."""
    from aiogram import Dispatcher, F, Router  # noqa: PLC0415 - only with Telegram
    from aiogram.filters import Command  # noqa: PLC0415

    me = await bot.get_me()
    router = Router()

    def sender(update: Message | CallbackQuery) -> int | None:
        return update.from_user.id if update.from_user else None

    @router.message(Command("status", "budget", "help", "start", "new", "cancel", "tier"))
    async def on_command(message: Message) -> None:
        if operator.authorized(message.chat.id, sender(message)):
            await message.answer(await operator.command(message.text or ""))

    @router.message(F.reply_to_message)
    async def on_reply(message: Message) -> None:
        replied = message.reply_to_message
        if not operator.authorized(message.chat.id, sender(message)) or replied is None:
            return
        if replied.from_user is None or replied.from_user.id != me.id:
            return
        result = await operator.reply(replied.text or "", message.text or "")
        await message.reply(result or "That message is not a question I can pass on.")

    @router.message()
    async def on_other(message: Message) -> None:
        # Plain text or an unknown command: the assistant's, when there is one (ADR 0021);
        # else say where answers go instead of dropping it.
        if not operator.authorized(message.chat.id, sender(message)):
            return
        text = message.text or ""
        if operator.assistant is not None and not text.startswith("/"):
            async with _typing(bot, message.chat.id):
                await message.answer(await operator.command(text))
            return
        await message.answer(await operator.command(text))

    @router.callback_query()
    async def on_button(query: CallbackQuery) -> None:
        chat_id = query.message.chat.id if query.message else None
        if not operator.authorized(chat_id, sender(query)):
            await query.answer()
            return
        result = await operator.button(query.data or "")
        final = result in _DONE.values() or result.startswith(
            (PLAN_STARTED, PLAN_CANCELLED, TASK_CANCELLED, "Project ")
        )
        await query.answer(result, show_alert=not final)
        if query.message is not None and final:
            # The buttons are spent; the answer stays visible as a reply.
            await bot.edit_message_reply_markup(
                chat_id=query.message.chat.id, message_id=query.message.message_id
            )
            await bot.send_message(
                query.message.chat.id,
                result,
                reply_to_message_id=query.message.message_id,
                disable_notification=True,  # the operator just pressed the button
            )

    dispatcher = Dispatcher()
    dispatcher.include_router(router)
    await dispatcher.start_polling(bot, handle_signals=False, close_bot_session=False)
