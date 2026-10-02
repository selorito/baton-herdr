from __future__ import annotations

import logging

import pytest
from pydantic import SecretStr

from coban.core.config import TelegramSettings
from coban.core.fakes import RecordingNotifier
from coban.core.model import TaskId
from coban.core.notify import LoggingNotifier, Notice, NoticeKind, Notifier
from coban.telegram.notifier import (
    MAX_MESSAGE_LENGTH,
    TelegramNotifier,
    format_notice,
    telegram_notifier,
)

NOTICE = Notice(NoticeKind.AGENT_LIMITED, TaskId("t1"), "Claude hit its limit; moved to Codex.")


async def test_telegram_notifier_sends_to_the_configured_chat_only() -> None:
    sent: list[tuple[int, str]] = []

    async def send(chat_id: int, text: str) -> None:
        sent.append((chat_id, text))

    notifier: Notifier = TelegramNotifier(send, chat_id=42)
    await notifier.notify(NOTICE)

    assert sent == [(42, "[t1] Claude hit its limit; moved to Codex.")]


async def test_a_failed_delivery_does_not_raise() -> None:
    async def broken(_chat_id: int, _text: str) -> None:
        raise ConnectionError

    await TelegramNotifier(broken, chat_id=42).notify(NOTICE)


def test_long_notices_are_cut_to_telegrams_limit() -> None:
    notice = Notice(NoticeKind.NEEDS_HUMAN, TaskId("t1"), "x" * 5000)
    text = format_notice(notice)
    assert len(text) == MAX_MESSAGE_LENGTH
    assert text.endswith("…")


@pytest.mark.parametrize(
    "settings",
    [
        TelegramSettings(),
        TelegramSettings(bot_token=SecretStr("123:abc")),  # no chat: nowhere to send
        TelegramSettings(chat_id=42),  # no token
    ],
)
def test_telegram_is_off_unless_both_token_and_chat_are_set(settings: TelegramSettings) -> None:
    assert telegram_notifier(settings) is None


async def test_telegram_notifier_is_built_from_settings_without_network() -> None:
    notifier = telegram_notifier(TelegramSettings(bot_token=SecretStr("123:abc"), chat_id=42))
    assert isinstance(notifier, TelegramNotifier)
    await notifier.aclose()


async def test_recording_and_logging_notifiers(caplog: pytest.LogCaptureFixture) -> None:
    recorder = RecordingNotifier()
    await recorder.notify(NOTICE)
    assert recorder.kinds == [NoticeKind.AGENT_LIMITED]
    with caplog.at_level(logging.INFO):
        await LoggingNotifier().notify(NOTICE)
