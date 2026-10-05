from __future__ import annotations

import logging
from dataclasses import replace

import pytest
from pydantic import SecretStr

from baton_herdr.core.config import TelegramSettings
from baton_herdr.core.fakes import RecordingNotifier
from baton_herdr.core.model import OperatorAction, TaskId
from baton_herdr.core.notify import LoggingNotifier, Notice, NoticeKind, Notifier
from baton_herdr.telegram.notifier import (
    ANSWER_HINT,
    MAX_MESSAGE_LENGTH,
    Buttons,
    TelegramNotifier,
    as_shown,
    format_notice,
    notice_buttons,
    parse_reference,
    telegram_notifier,
)

NOTICE = Notice(NoticeKind.TASK_HANDED_OFF, TaskId("t1"), "Claude hit its limit; moved to Codex.")


async def test_telegram_notifier_sends_to_the_configured_chat_only() -> None:
    sent: list[tuple[int, str, Buttons, bool]] = []

    async def send(chat_id: int, text: str, buttons: Buttons, silent: bool) -> None:
        sent.append((chat_id, text, buttons, silent))

    notifier: Notifier = TelegramNotifier(send, chat_id=42)
    await notifier.notify(NOTICE)

    assert sent == [(42, "Claude hit its limit; moved to Codex.\n\n<code>t1</code>", [], True)]


@pytest.mark.parametrize("kind", list(NoticeKind))
async def test_only_decisions_and_a_lack_of_agents_make_a_sound(kind: NoticeKind) -> None:
    silent: list[bool] = []

    async def send(_chat_id: int, _text: str, _buttons: Buttons, quiet: bool) -> None:
        silent.append(quiet)

    await TelegramNotifier(send, chat_id=42).notify(replace(NOTICE, kind=kind))

    loud = {NoticeKind.NEEDS_HUMAN, NoticeKind.WAITING_FOR_AGENT}
    assert silent == [kind not in loud]


def test_a_decision_names_its_blocker_and_offers_only_the_actions_that_fit() -> None:
    permission = Notice(
        NoticeKind.NEEDS_HUMAN,
        TaskId("t-1a2b3c4d"),
        "claude is waiting for a decision.",
        blocker_seq=42,
        actions=(OperatorAction.APPROVE, OperatorAction.DENY),
    )
    assert format_notice(permission) == (
        "claude is waiting for a decision.\n\n<code>t-1a2b3c4d #42</code>"
    )
    assert notice_buttons(permission) == [
        ("Approve", "approve:t-1a2b3c4d:42"),
        ("Deny", "deny:t-1a2b3c4d:42"),
    ]
    assert all(len(data.encode()) <= 64 for _, data in notice_buttons(permission))

    question = replace(permission, actions=(OperatorAction.ANSWER,))
    assert format_notice(question).endswith(f"\n\n{ANSWER_HINT}\n<code>t-1a2b3c4d #42</code>")
    assert notice_buttons(question) == []


@pytest.mark.parametrize(
    ("shown", "reference"),
    [
        # What Telegram returns as the replied message's text: the HTML without tags.
        (
            "Fix divide\nclaude asks: raise?\n\nReply to this message to answer.\nt-1a2b #42",
            ("t-1a2b", 42),
        ),
        ("[t-1a2b #42] claude is waiting for a decision.", ("t-1a2b", 42)),  # before 2026-10
        ("Fix divide\nDone on claude.\n\nt-1a2b", None),  # no blocker
        ("t-1a2b #42 is mentioned\nin the middle", None),
    ],
)
def test_a_reply_finds_the_blocker_in_the_notice_it_answers(
    shown: str, reference: tuple[str, int] | None
) -> None:
    assert parse_reference(shown) == reference


async def test_a_failed_delivery_does_not_raise() -> None:
    async def broken(_chat_id: int, _text: str, _buttons: Buttons, _silent: bool) -> None:
        raise ConnectionError

    await TelegramNotifier(broken, chat_id=42).notify(NOTICE)


def test_long_notices_are_cut_to_telegrams_limit_blocks_first() -> None:
    notice = Notice(
        NoticeKind.NEEDS_HUMAN, TaskId("t1"), "x <" * 2000, title="T", blocks=("screen",)
    )
    text = format_notice(notice)
    shown = as_shown(text)
    assert len(shown) == MAX_MESSAGE_LENGTH
    assert "<pre>" not in text
    assert shown.endswith("…\n\nt1")
    assert "&lt;" in text  # still escaped after cutting


def test_every_part_is_escaped_and_secrets_never_leave() -> None:
    notice = Notice(
        NoticeKind.TASK_STOPPED,
        TaskId("t1"),
        "codex <crashed> & left",
        title="Fix <b>bold</b>",
        details=("OPENAI_API_KEY=sk-proj-abcdefghijklmnopqrstuvwx",),
        blocks=("$ curl -H 'Authorization: Bearer abcdefghijklmnop1234'\n123456789:AAH-bot",),
    )
    text = format_notice(notice, secrets=("123456789:AAH-bot",))
    assert text == (
        "<b>Fix &lt;b&gt;bold&lt;/b&gt;</b>\n"
        "codex &lt;crashed&gt; &amp; left\n"
        "OPENAI_API_KEY=&lt;redacted&gt;\n\n"
        "<pre>$ curl -H 'Authorization: &lt;redacted&gt;'\n&lt;redacted&gt;</pre>\n\n"
        "<code>t1</code>"
    )


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
    assert recorder.kinds == [NoticeKind.TASK_HANDED_OFF]
    with caplog.at_level(logging.INFO):
        await LoggingNotifier().notify(NOTICE)
