"""Authenticated Telegram webhook ingress acceptance tests."""

from aiogram import Bot
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
from pydantic import SecretStr

from app.api.telegram import router
from app.config import Settings
from app.services.telegram_update_inbox import (
    TelegramAcceptOutcome,
    TelegramAcceptResult,
)


class RecordingInbox:
    def __init__(self) -> None:
        self.accepted: list[tuple[int, int | None, dict[str, object]]] = []

    async def accept(
        self,
        update_id: int,
        telegram_user_id: int | None,
        payload: dict[str, object],
    ) -> TelegramAcceptResult:
        self.accepted.append((update_id, telegram_user_id, payload))
        return TelegramAcceptResult(TelegramAcceptOutcome.ACCEPTED, "pending")


def webhook_app(
    settings: Settings,
    inbox: RecordingInbox,
    bot: Bot,
    *,
    max_bytes: int = 4096,
) -> FastAPI:
    app = FastAPI()
    app.state.settings = settings
    app.state.telegram_webhook_max_bytes = max_bytes
    app.state.telegram_bot = bot
    app.state.telegram_update_inbox = inbox
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


async def test_valid_webhook_enqueues_without_running_dispatcher(settings: Settings) -> None:
    configured = settings.model_copy(
        update={
            "telegram_webhook_url": "https://example.com/telegram/webhook",
            "telegram_webhook_secret": SecretStr("safe-webhook-secret"),
        }
    )
    inbox = RecordingInbox()
    bot = Bot(token=configured.telegram_bot_token.get_secret_value())
    try:
        app = webhook_app(configured, inbox, bot)
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            response = await client.post(
                "/telegram/webhook",
                headers={"X-Telegram-Bot-Api-Secret-Token": "safe-webhook-secret"},
                json=update_payload(),
            )
        assert response.status_code == 204
        assert len(inbox.accepted) == 1
        update_id, telegram_user_id, payload = inbox.accepted[0]
        assert update_id == 101
        assert telegram_user_id == 42
        assert payload["update_id"] == 101
    finally:
        await bot.session.close()


async def test_webhook_rejects_wrong_secret_before_enqueue(settings: Settings) -> None:
    configured = settings.model_copy(
        update={
            "telegram_webhook_url": "https://example.com/telegram/webhook",
            "telegram_webhook_secret": SecretStr("expected-secret"),
        }
    )
    inbox = RecordingInbox()
    bot = Bot(token=configured.telegram_bot_token.get_secret_value())
    try:
        app = webhook_app(configured, inbox, bot)
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            response = await client.post(
                "/telegram/webhook",
                headers={"X-Telegram-Bot-Api-Secret-Token": "wrong-secret"},
                json=update_payload(),
            )
        assert response.status_code == 401
        assert inbox.accepted == []
    finally:
        await bot.session.close()


async def test_webhook_is_hidden_when_disabled(settings: Settings) -> None:
    inbox = RecordingInbox()
    bot = Bot(token=settings.telegram_bot_token.get_secret_value())
    try:
        app = webhook_app(settings, inbox, bot)
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            response = await client.post("/telegram/webhook", json=update_payload())
        assert response.status_code == 404
        assert inbox.accepted == []
    finally:
        await bot.session.close()


async def test_webhook_rejects_oversized_and_invalid_updates(settings: Settings) -> None:
    configured = settings.model_copy(
        update={
            "telegram_webhook_url": "https://example.com/telegram/webhook",
            "telegram_webhook_secret": SecretStr("safe-webhook-secret"),
        }
    )
    inbox = RecordingInbox()
    bot = Bot(token=configured.telegram_bot_token.get_secret_value())
    headers = {"X-Telegram-Bot-Api-Secret-Token": "safe-webhook-secret"}
    try:
        app = webhook_app(configured, inbox, bot, max_bytes=64)
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            oversized = await client.post("/telegram/webhook", headers=headers, content=b"x" * 65)
            invalid = await client.post(
                "/telegram/webhook",
                headers=headers,
                content=b"{}",
            )
        assert oversized.status_code == 413
        assert invalid.status_code == 400
        assert inbox.accepted == []
    finally:
        await bot.session.close()
