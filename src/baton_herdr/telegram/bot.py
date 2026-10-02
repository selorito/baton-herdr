"""The operator's side of the Telegram bot: status, buttons and answers (ADR 0009).

``OperatorBot`` holds the logic and knows nothing about aiogram, so it is tested
directly; ``run_bot`` connects it to Telegram with long polling.

Only updates from the configured chat *and* the configured owner are handled;
anything else is dropped without a reply. Every action goes through
``baton_herdr.scheduler.operator.act``, which checks that the blocker is still open and
that the pane hosts the attempt before anything is typed.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

from baton_herdr.budget.availability import fold_availability
from baton_herdr.core.logging import get_logger
from baton_herdr.core.model import OperatorAction, TaskId
from baton_herdr.core.projection import project
from baton_herdr.scheduler.operator import ActionRefusedError, act, find_blocker
from baton_herdr.telegram.notifier import parse_reference

if TYPE_CHECKING:
    from collections.abc import Callable, Mapping
    from datetime import timedelta

    from aiogram import Bot
    from aiogram.types import CallbackQuery, Message

    from baton_herdr.core.agents import AgentAdapter
    from baton_herdr.core.model import AgentKind
    from baton_herdr.core.panes import PaneHost
    from baton_herdr.core.ports import Clock, EventStore

HELP = (
    "/status: open tasks and agents.\n"
    "Approve or Deny under a permission prompt; reply to a question to answer it."
)
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


class OperatorBot:
    def __init__(  # noqa: PLR0913 - every collaborator is a separate port
        self,
        *,
        store: EventStore,
        host: PaneHost,
        adapters: Mapping[AgentKind, AgentAdapter],
        clock: Clock,
        settings: BotSettings,
        on_action: Callable[[], None] | None = None,
    ) -> None:
        self._store = store
        self._host = host
        self._adapters = adapters
        self._clock = clock
        self._settings = settings
        # Called after an action was carried out, so the daemon looks at the task now.
        self._on_action = on_action or (lambda: None)
        self._log = get_logger("baton.telegram")

    def authorized(self, chat_id: int | None, user_id: int | None) -> bool:
        allowed = chat_id == self._settings.chat_id and user_id == self._settings.owner_id
        if not allowed:
            self._log.warning("telegram update ignored", chat_id=chat_id, user_id=user_id)
        return allowed

    async def command(self, text: str) -> str:
        name = text.split(maxsplit=1)[0].split("@", maxsplit=1)[0] if text.strip() else ""
        if name == "/status":
            return await self.status()
        return HELP

    async def status(self) -> str:
        events = await self._store.read()
        board = project(events)
        lines = []
        for task in board.tasks.values():
            if task.status.is_terminal:
                continue
            agent = task.attempts[-1].agent.value if task.attempts else "-"
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
                f"{agent.value}: limited until {until:%H:%M} UTC"
                if limited and until is not None
                else f"{agent.value}: available"
            )
        return "\n".join([*(lines or ["No open tasks."]), "", *agents])

    async def button(self, data: str) -> str:
        """A press on Approve / Deny; ``data`` is ``action:task:seq``."""
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


async def run_bot(bot: Bot, operator: OperatorBot) -> None:
    """Long-poll Telegram and hand authorised updates to ``operator`` until cancelled."""
    from aiogram import Dispatcher, F, Router  # noqa: PLC0415 - only with Telegram
    from aiogram.filters import Command  # noqa: PLC0415

    me = await bot.get_me()
    router = Router()

    def sender(update: Message | CallbackQuery) -> int | None:
        return update.from_user.id if update.from_user else None

    @router.message(Command("status", "help", "start"))
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

    @router.callback_query()
    async def on_button(query: CallbackQuery) -> None:
        chat_id = query.message.chat.id if query.message else None
        if not operator.authorized(chat_id, sender(query)):
            await query.answer()
            return
        result = await operator.button(query.data or "")
        await query.answer(result, show_alert=result not in _DONE.values())
        if query.message is not None and result in _DONE.values():
            # The buttons are spent; the answer stays visible as a reply.
            await bot.edit_message_reply_markup(
                chat_id=query.message.chat.id, message_id=query.message.message_id
            )
            await bot.send_message(
                query.message.chat.id, result, reply_to_message_id=query.message.message_id
            )

    dispatcher = Dispatcher()
    dispatcher.include_router(router)
    await dispatcher.start_polling(bot, handle_signals=False, close_bot_session=False)
