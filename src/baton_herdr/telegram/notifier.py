"""Send notices to one Telegram chat.

Messages go to the single configured chat; failures are logged and swallowed,
because a task must not fail when Telegram is unreachable. Notices are Telegram
HTML: the task's name in bold, what happened, details, monospace blocks, and the
task id at the end. A notice that asks for a decision ends with its blocker
(``t-1a2b3c4d #42``), which is how a reply finds it, and, where an action fits,
carries buttons bound to that blocker (ADR 0009). Secrets are masked first.
"""

from __future__ import annotations

import html
import re
from typing import TYPE_CHECKING

from baton_herdr.core.logging import get_logger
from baton_herdr.core.model import OperatorAction
from baton_herdr.core.redact import redact

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable, Iterable, Sequence

    from aiogram import Bot

    from baton_herdr.core.config import TelegramSettings
    from baton_herdr.core.notify import Notice

# (label, callback data) pairs, shown as one row of buttons.
type Buttons = Sequence[tuple[str, str]]
type SendMessage = Callable[[int, str, Buttons], Awaitable[object]]

MAX_MESSAGE_LENGTH = 4096  # Telegram's limit for one text message, counted without markup
ANSWER_HINT = "Reply to this message to answer."
_BUTTON_LABELS = {OperatorAction.APPROVE: "Approve", OperatorAction.DENY: "Deny"}
# The reference in a notice's last line ("t-1a2b3c4d #42"), or, in notices sent before
# 2026-10, at the start ("[t-1a2b3c4d #42] ...").
_FOOTER = re.compile(r"(?:^|\n)(?P<task>[\w-]+) #(?P<seq>\d+)\s*\Z")
_HEADER = re.compile(r"^\[(?P<task>[\w-]+) #(?P<seq>\d+)\]")


def format_notice(notice: Notice, *, secrets: Iterable[str] = ()) -> str:
    """The notice as Telegram HTML: the task's name first, its id last, secrets masked.

    Telegram cannot show grey or small text, so the id goes in ``<code>``: set apart,
    and copied with a tap for the CLI (``baton approve t-…``).
    """
    known = tuple(secrets)

    def clean(text: str) -> str:
        return html.escape(redact(text, known), quote=False)

    reference = f"{notice.task_id} #{notice.blocker_seq}" if notice.blocker_seq else notice.task_id
    head = [f"<b>{clean(notice.title)}</b>"] if notice.title else []
    head += [clean(notice.text), *(clean(line) for line in notice.details)]
    tail = []
    if OperatorAction.ANSWER in notice.actions and notice.blocker_seq:
        tail.append(ANSWER_HINT)
    tail.append(f"<code>{clean(reference)}</code>")
    blocks = [f"<pre>{clean(block)}</pre>" for block in notice.blocks]

    def message(head: list[str], blocks: list[str]) -> str:
        return "\n\n".join(["\n".join(head), *blocks, "\n".join(tail)])

    text = message(head, blocks)
    if _shown_length(text) > MAX_MESSAGE_LENGTH:
        text = message(head, [])  # the blocks go first: they are context, not the news
    while _shown_length(text) > MAX_MESSAGE_LENGTH and head:
        overflow = _shown_length(text) - MAX_MESSAGE_LENGTH
        last = html.unescape(head[-1])
        keep = len(last) - overflow - 1
        head = [*head[:-1], html.escape(last[:keep], quote=False) + "…"] if keep > 0 else head[:-1]
        text = message(head, [])
    return text


def as_shown(text: str) -> str:
    """HTML as Telegram shows it, and returns it as a message's text: no tags."""
    return html.unescape(re.sub(r"<[^>]+>", "", text))


def _shown_length(text: str) -> int:
    return len(as_shown(text))


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
    """The task and blocker a notice was about, from its plain text as Telegram returns it."""
    match = _FOOTER.search(message_text) or _HEADER.match(message_text)
    if match is None:
        return None
    return match["task"], int(match["seq"])


class TelegramNotifier:
    def __init__(
        self,
        send: SendMessage,
        chat_id: int,
        *,
        bot: Bot | None = None,
        secrets: Iterable[str] = (),
    ) -> None:
        self._send = send
        self._chat_id = chat_id
        self._bot = bot
        # Masked wherever they appear in a notice: the bot's own token.
        self._secrets = tuple(secrets)
        self._log = get_logger("baton.telegram")

    async def notify(self, notice: Notice) -> None:
        try:
            await self._send(
                self._chat_id,
                format_notice(notice, secrets=self._secrets),
                notice_buttons(notice),
            )
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
        return await bot.send_message(chat_id, text, reply_markup=markup, parse_mode="HTML")

    token = settings.bot_token.get_secret_value()
    return TelegramNotifier(send, settings.chat_id, bot=bot, secrets=(token,))
