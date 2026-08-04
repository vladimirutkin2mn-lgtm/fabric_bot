"""Telegram update worker state-transition tests."""

from typing import cast
from uuid import UUID, uuid4

from aiogram import Bot, Dispatcher
from aiogram.types import Update

from app.services.telegram_update_inbox import (
    ClaimedTelegramUpdate,
    TelegramUpdateInboxService,
)
from app.services.telegram_update_worker import TelegramUpdateWorker


class RecordingInbox:
    def __init__(self, claim: ClaimedTelegramUpdate | None) -> None:
        self.claim = claim
        self.completed: list[tuple[int, UUID]] = []
        self.retried: list[tuple[int, UUID, str]] = []
        self.failed: list[tuple[int, UUID, str]] = []

    async def claim_one(self, worker_id: str) -> ClaimedTelegramUpdate | None:
        assert worker_id == "worker"
        claim, self.claim = self.claim, None
        return claim

    async def complete(self, update_id: int, claim_id: UUID) -> bool:
        self.completed.append((update_id, claim_id))
        return True

    async def retry(self, update_id: int, claim_id: UUID, code: str) -> bool:
        self.retried.append((update_id, claim_id, code))
        return True

    async def fail_permanent(self, update_id: int, claim_id: UUID, code: str) -> bool:
        self.failed.append((update_id, claim_id, code))
        return True


class RecordingDispatcher:
    def __init__(self, *, fail: bool = False) -> None:
        self.fail = fail
        self.updates: list[int] = []

    async def feed_update(self, bot: Bot, update: Update) -> None:
        assert update.bot is bot
        if self.fail:
            raise RuntimeError("safe test failure")
        self.updates.append(update.update_id)


def valid_payload(update_id: int) -> dict[str, object]:
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


async def test_worker_completes_successful_update() -> None:
    claim_id = uuid4()
    inbox = RecordingInbox(ClaimedTelegramUpdate(2001, claim_id, valid_payload(2001)))
    dispatcher = RecordingDispatcher()
    bot = Bot(token="123456789:AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA")
    try:
        worker = TelegramUpdateWorker(
            cast(TelegramUpdateInboxService, inbox),
            bot,
            cast(Dispatcher, dispatcher),
        )
        assert await worker.run_once("worker")
        assert dispatcher.updates == [2001]
        assert inbox.completed == [(2001, claim_id)]
        assert inbox.retried == []
    finally:
        await bot.session.close()


async def test_worker_retries_unexpected_handler_failure() -> None:
    claim_id = uuid4()
    inbox = RecordingInbox(ClaimedTelegramUpdate(2002, claim_id, valid_payload(2002)))
    dispatcher = RecordingDispatcher(fail=True)
    bot = Bot(token="123456789:AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA")
    try:
        worker = TelegramUpdateWorker(
            cast(TelegramUpdateInboxService, inbox),
            bot,
            cast(Dispatcher, dispatcher),
        )
        assert await worker.run_once("worker")
        assert inbox.retried == [(2002, claim_id, "unexpected_handler_error")]
        assert inbox.completed == []
    finally:
        await bot.session.close()


async def test_worker_permanently_rejects_invalid_decrypted_update() -> None:
    claim_id = uuid4()
    inbox = RecordingInbox(ClaimedTelegramUpdate(2003, claim_id, {}))
    dispatcher = RecordingDispatcher()
    bot = Bot(token="123456789:AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA")
    try:
        worker = TelegramUpdateWorker(
            cast(TelegramUpdateInboxService, inbox),
            bot,
            cast(Dispatcher, dispatcher),
        )
        assert await worker.run_once("worker")
        assert inbox.failed == [(2003, claim_id, "invalid_telegram_update")]
        assert dispatcher.updates == []
    finally:
        await bot.session.close()
