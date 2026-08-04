"""At-least-once Telegram update execution outside the HTTP request."""

import asyncio
import logging

from aiogram import Bot, Dispatcher
from aiogram.types import Update
from pydantic import ValidationError

from app.services.telegram_update_inbox import TelegramUpdateInboxService

logger = logging.getLogger(__name__)


class TelegramUpdateWorker:
    def __init__(
        self,
        inbox: TelegramUpdateInboxService,
        bot: Bot,
        dispatcher: Dispatcher,
    ) -> None:
        self._inbox = inbox
        self._bot = bot
        self._dispatcher = dispatcher

    async def run_once(self, worker_id: str) -> bool:
        claim = await self._inbox.claim_one(worker_id)
        if claim is None:
            return False
        try:
            update = Update.model_validate(claim.payload, context={"bot": self._bot})
        except (ValidationError, TypeError):
            await self._inbox.fail_permanent(
                claim.update_id, claim.claim_id, "invalid_telegram_update"
            )
            return True
        try:
            await self._dispatcher.feed_update(self._bot, update)
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("telegram_update_failed update_id=%s", claim.update_id)
            await self._inbox.retry(
                claim.update_id, claim.claim_id, "unexpected_handler_error"
            )
            return True
        await self._inbox.complete(claim.update_id, claim.claim_id)
        return True
