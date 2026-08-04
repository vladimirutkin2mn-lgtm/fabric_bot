"""Authenticated Telegram webhook transport acceptance tests."""

from typing import cast

from aiogram import Bot, Dispatcher
from aiogram.types import Update
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
from pydantic import SecretStr

from app.api.telegram import router
from app.config import Settings


class RecordingDispatcher:
    def __init__(self) -> None:
        self.updates: list[Update] = []

    async def feed_update(self, bot: Bot, update: Update) -> None:
        assert update.bot is bot
        self.updates.append(update)


def webhook_app(
    settings: Settings,
    dispatcher: RecordingDispatcher,
    bot: Bot,
    *,
    max_bytes: int = 4096,
) -> FastAPI:
    app = FastAPI()
    app.state.settings = settings
    app.state.telegram_webhook_max_bytes = max_bytes
    app.state.telegram_bot = bot
    app.state.telegram_dispatcher = cast(Dispatcher, dispatcher)
    app.include_router(router)
    return app


def update_payload(update_id: int = 101) -> dict[str, object]:
    return {
        "update_id": update_id,
        "message": {
            "message_id": 1,
            "date": 1_700_000_000,
            "chat": {"id": 42, "type": "private"},
            "from": {"id": 42, "is_bot": False, "first_name": "Test"},
            "text": "/start",
        },
    }


async def test_valid_webhook_feeds_one_update(settings: Settings) -> None:
    configured = settings.model_copy(
        update={
            "telegram_webhook_url": "https://example.com/telegram/webhook",
            "telegram_webhook_secret": SecretStr("safe-webhook-secret"),
        }
    )
    dispatcher = RecordingDispatcher()
    bot = Bot(token=configured.telegram_bot_token.get_secret_value())
    try:
        app = webhook_app(configured, dispatcher, bot)
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            response = await client.post(
                "/telegram/webhook",
                headers={"X-Telegram-Bot-Api-Secret-Token": "safe-webhook-secret"},
                json=update_payload(),
            )
        assert response.status_code == 204
        assert [update.update_id for update in dispatcher.updates] == [101]
    finally:
        await bot.session.close()


async def test_webhook_rejects_wrong_secret_before_dispatch(settings: Settings) -> None:
    configured = settings.model_copy(
        update={
            "telegram_webhook_url": "https://example.com/telegram/webhook",
            "telegram_webhook_secret": SecretStr("expected-secret"),
        }
    )
    dispatcher = RecordingDispatcher()
    bot = Bot(token=configured.telegram_bot_token.get_secret_value())
    try:
        app = webhook_app(configured, dispatcher, bot)
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            response = await client.post(
                "/telegram/webhook",
                headers={"X-Telegram-Bot-Api-Secret-Token": "wrong-secret"},
                json=update_payload(),
            )
        assert response.status_code == 401
        assert dispatcher.updates == []
    finally:
        await bot.session.close()


async def test_webhook_is_hidden_when_disabled(settings: Settings) -> None:
    dispatcher = RecordingDispatcher()
    bot = Bot(token=settings.telegram_bot_token.get_secret_value())
    try:
        app = webhook_app(settings, dispatcher, bot)
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            response = await client.post("/telegram/webhook", json=update_payload())
        assert response.status_code == 404
        assert dispatcher.updates == []
    finally:
        await bot.session.close()


async def test_webhook_rejects_oversized_and_invalid_updates(settings: Settings) -> None:
    configured = settings.model_copy(
        update={
            "telegram_webhook_url": "https://example.com/telegram/webhook",
            "telegram_webhook_secret": SecretStr("safe-webhook-secret"),
        }
    )
    dispatcher = RecordingDispatcher()
    bot = Bot(token=configured.telegram_bot_token.get_secret_value())
    headers = {"X-Telegram-Bot-Api-Secret-Token": "safe-webhook-secret"}
    try:
        app = webhook_app(configured, dispatcher, bot, max_bytes=64)
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            oversized = await client.post("/telegram/webhook", headers=headers, content=b"x" * 65)
            invalid = await client.post(
                "/telegram/webhook",
                headers=headers,
                content=b"{}",
            )
        assert oversized.status_code == 413
        assert invalid.status_code == 400
        assert dispatcher.updates == []
    finally:
        await bot.session.close()
