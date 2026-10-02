"""Send notices to one Telegram chat.

Delivery only: no commands, no buttons yet (roadmap step 4). Messages go to the
single configured chat; failures are logged and swallowed, because a task must
not fail when Telegram is unreachable.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from coban.core.logging import get_logger

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable

    from aiogram import Bot

    from coban.core.config import TelegramSettings
    from coban.core.notify import Notice

type SendMessage = Callable[[int, str], Awaitable[object]]

MAX_MESSAGE_LENGTH = 4096  # Telegram's limit for one text message


def format_notice(notice: Notice) -> str:
    text = f"[{notice.task_id}] {notice.text}"
    if len(text) > MAX_MESSAGE_LENGTH:
        text = text[: MAX_MESSAGE_LENGTH - 1] + "…"
    return text


class TelegramNotifier:
    def __init__(self, send: SendMessage, chat_id: int, *, bot: Bot | None = None) -> None:
        self._send = send
        self._chat_id = chat_id
        self._bot = bot
        self._log = get_logger("coban.telegram")

    async def notify(self, notice: Notice) -> None:
        try:
            await self._send(self._chat_id, format_notice(notice))
        except Exception as err:  # noqa: BLE001 - delivery must never break a task
            self._log.warning(
                "telegram delivery failed", kind=notice.kind.value, error=type(err).__name__
            )

    async def aclose(self) -> None:
        if self._bot is not None:
            await self._bot.session.close()


def telegram_notifier(settings: TelegramSettings) -> TelegramNotifier | None:
    """A notifier for the configured chat, or ``None`` when Telegram is not configured."""
    if settings.bot_token is None or settings.chat_id is None:
        return None
    from aiogram import Bot  # noqa: PLC0415 - only import aiogram when Telegram is used

    bot = Bot(token=settings.bot_token.get_secret_value())
    return TelegramNotifier(bot.send_message, settings.chat_id, bot=bot)
