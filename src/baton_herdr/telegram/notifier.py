"""Send notices to one Telegram chat.

Messages go to the single configured chat; failures are logged and swallowed,
because a task must not fail when Telegram is unreachable. A notice that asks
for a decision carries its blocker in the header (``[t-1 #42]``) and, where an
action fits, buttons bound to that blocker (ADR 0009).
"""

from __future__ import annotations

import re
from typing import TYPE_CHECKING

from baton_herdr.core.logging import get_logger
from baton_herdr.core.model import OperatorAction

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable, Sequence

    from aiogram import Bot

    from baton_herdr.core.config import TelegramSettings
    from baton_herdr.core.notify import Notice

# (label, callback data) pairs, shown as one row of buttons.
type Buttons = Sequence[tuple[str, str]]
type SendMessage = Callable[[int, str, Buttons], Awaitable[object]]

MAX_MESSAGE_LENGTH = 4096  # Telegram's limit for one text message
ANSWER_HINT = "Reply to this message to answer."
_BUTTON_LABELS = {OperatorAction.APPROVE: "Approve", OperatorAction.DENY: "Deny"}
_HEADER = re.compile(r"^\[(?P<task>[\w-]+) #(?P<seq>\d+)\]")


def format_notice(notice: Notice) -> str:
    ref = f"{notice.task_id} #{notice.blocker_seq}" if notice.blocker_seq else notice.task_id
    text = f"[{ref}] {notice.text}"
    if OperatorAction.ANSWER in notice.actions and notice.blocker_seq:
        text += f"\n\n{ANSWER_HINT}"
    if len(text) > MAX_MESSAGE_LENGTH:
        text = text[: MAX_MESSAGE_LENGTH - 1] + "…"
    return text


def notice_buttons(notice: Notice) -> list[tuple[str, str]]:
    if notice.blocker_seq is None:
        return []
    return [
        (label, button_data(action, notice.task_id, notice.blocker_seq))
        for action, label in _BUTTON_LABELS.items()
        if action in notice.actions
    ]


def button_data(action: OperatorAction, task_id: str, blocker_seq: int) -> str:
    """Callback data, at most 64 bytes for Telegram: ``approve:t-1a2b3c4d:42``."""
    return f"{action.value}:{task_id}:{blocker_seq}"


def parse_reference(message_text: str) -> tuple[str, int] | None:
    """The task and blocker a notice was about, read from its header."""
    match = _HEADER.match(message_text)
    if match is None:
        return None
    return match["task"], int(match["seq"])


class TelegramNotifier:
    def __init__(self, send: SendMessage, chat_id: int, *, bot: Bot | None = None) -> None:
        self._send = send
        self._chat_id = chat_id
        self._bot = bot
        self._log = get_logger("baton.telegram")

    async def notify(self, notice: Notice) -> None:
        try:
            await self._send(self._chat_id, format_notice(notice), notice_buttons(notice))
        except Exception as err:  # noqa: BLE001 - delivery must never break a task
            self._log.warning(
                "telegram delivery failed", kind=notice.kind.value, error=type(err).__name__
            )

    @property
    def bot(self) -> Bot | None:
        return self._bot

    async def aclose(self) -> None:
        if self._bot is not None:
            await self._bot.session.close()


def telegram_notifier(settings: TelegramSettings) -> TelegramNotifier | None:
    """A notifier for the configured chat, or ``None`` when Telegram is not configured."""
    if settings.bot_token is None or settings.chat_id is None:
        return None
    from aiogram import Bot  # noqa: PLC0415 - only import aiogram when Telegram is used
    from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup  # noqa: PLC0415

    bot = Bot(token=settings.bot_token.get_secret_value())

    async def send(chat_id: int, text: str, buttons: Buttons) -> object:
        markup = (
            InlineKeyboardMarkup(
                inline_keyboard=[
                    [
                        InlineKeyboardButton(text=label, callback_data=data)
                        for label, data in buttons
                    ]
                ]
            )
            if buttons
            else None
        )
        return await bot.send_message(chat_id, text, reply_markup=markup)

    return TelegramNotifier(send, settings.chat_id, bot=bot)
