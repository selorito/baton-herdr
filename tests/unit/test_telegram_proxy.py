"""Telegram behind a proxy: aiohttp reads no proxy variable by itself, so baton passes it."""

from __future__ import annotations

from aiogram.client.session.aiohttp import AiohttpSession
from pydantic import SecretStr

from baton_herdr.core.config import TelegramSettings, telegram_proxy
from baton_herdr.telegram.notifier import telegram_bot

PROXY = "http://proxy.corp:3128"


def test_the_environment_s_proxy_is_used() -> None:
    assert telegram_proxy(TelegramSettings(), {"HTTPS_PROXY": PROXY}) == PROXY
    assert telegram_proxy(TelegramSettings(), {"https_proxy": PROXY}) == PROXY
    assert telegram_proxy(TelegramSettings(), {"ALL_PROXY": PROXY}) == PROXY
    assert telegram_proxy(TelegramSettings(), {}) is None


def test_no_proxy_naming_telegram_bypasses_it() -> None:
    env = {"HTTPS_PROXY": PROXY, "NO_PROXY": "localhost, .telegram.org"}
    assert telegram_proxy(TelegramSettings(), env) is None
    assert telegram_proxy(TelegramSettings(), {**env, "NO_PROXY": "*"}) is None
    assert telegram_proxy(TelegramSettings(), {**env, "NO_PROXY": "example.com"}) == PROXY


def test_the_setting_wins_and_empty_means_none() -> None:
    env = {"HTTPS_PROXY": PROXY}
    assert telegram_proxy(TelegramSettings(proxy="http://other:8080"), env) == "http://other:8080"
    assert telegram_proxy(TelegramSettings(proxy=""), env) is None


async def test_the_bot_goes_through_the_proxy() -> None:
    bot = telegram_bot(TelegramSettings(bot_token=SecretStr("1:x"), proxy=PROXY))
    try:
        assert isinstance(bot.session, AiohttpSession)
        assert bot.session.proxy == PROXY
    finally:
        await bot.session.close()
