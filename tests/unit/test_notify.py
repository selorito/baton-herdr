from __future__ import annotations

import logging
from dataclasses import replace

import pytest
from pydantic import SecretStr

from coban.core.config import TelegramSettings
from coban.core.fakes import RecordingNotifier
from coban.core.model import OperatorAction, TaskId
from coban.core.notify import LoggingNotifier, Notice, NoticeKind, Notifier
from coban.telegram.notifier import (
    ANSWER_HINT,
    MAX_MESSAGE_LENGTH,
    Buttons,
    TelegramNotifier,
    format_notice,
    notice_buttons,
    parse_reference,
    telegram_notifier,
)

NOTICE = Notice(NoticeKind.AGENT_LIMITED, TaskId("t1"), "Claude hit its limit; moved to Codex.")


async def test_telegram_notifier_sends_to_the_configured_chat_only() -> None:
    sent: list[tuple[int, str, Buttons]] = []

    async def send(chat_id: int, text: str, buttons: Buttons) -> None:
        sent.append((chat_id, text, buttons))

    notifier: Notifier = TelegramNotifier(send, chat_id=42)
    await notifier.notify(NOTICE)

    assert sent == [(42, "[t1] Claude hit its limit; moved to Codex.", [])]


def test_a_decision_names_its_blocker_and_offers_only_the_actions_that_fit() -> None:
    permission = Notice(
        NoticeKind.NEEDS_HUMAN,
        TaskId("t-1a2b3c4d"),
        "claude is waiting for a decision.",
        blocker_seq=42,
        actions=(OperatorAction.APPROVE, OperatorAction.DENY),
    )
    assert format_notice(permission) == "[t-1a2b3c4d #42] claude is waiting for a decision."
    assert notice_buttons(permission) == [
        ("Approve", "approve:t-1a2b3c4d:42"),
        ("Deny", "deny:t-1a2b3c4d:42"),
    ]
    assert all(len(data.encode()) <= 64 for _, data in notice_buttons(permission))

    question = replace(permission, actions=(OperatorAction.ANSWER,))
    assert format_notice(question).endswith(f"\n\n{ANSWER_HINT}")
    assert notice_buttons(question) == []
    assert parse_reference(format_notice(question)) == ("t-1a2b3c4d", 42)
    assert parse_reference(format_notice(NOTICE)) is None


async def test_a_failed_delivery_does_not_raise() -> None:
    async def broken(_chat_id: int, _text: str, _buttons: Buttons) -> None:
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
