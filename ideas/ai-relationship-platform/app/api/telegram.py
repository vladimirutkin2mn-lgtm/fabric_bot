"""Authenticated Telegram webhook transport."""

import hmac
import json
import logging
from typing import Annotated, cast

from aiogram import Bot
from aiogram.types import Update
from fastapi import APIRouter, Header, HTTPException, Request, Response, status
from pydantic import ValidationError

from app.config import Settings
from app.services.telegram_update_inbox import (
    TelegramAcceptOutcome,
    TelegramUpdateInboxService,
)

router = APIRouter(tags=["telegram"])
logger = logging.getLogger(__name__)


def _telegram_user_id(update: Update) -> int | None:
    if update.message is not None and update.message.from_user is not None:
        return update.message.from_user.id
    if update.callback_query is not None:
        return update.callback_query.from_user.id
    return None


@router.post("/telegram/webhook", status_code=status.HTTP_204_NO_CONTENT)
async def telegram_webhook(
    request: Request,
    telegram_secret: Annotated[str | None, Header(alias="X-Telegram-Bot-Api-Secret-Token")] = None,
) -> Response:
    """Authenticate, validate, durably enqueue, and acknowledge without running handlers."""
    settings = cast(Settings, request.app.state.settings)
    if not settings.webhook_enabled:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Not found")
    expected = settings.telegram_webhook_secret.get_secret_value()
    supplied = telegram_secret or ""
    if not hmac.compare_digest(supplied.encode(), expected.encode()):
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Unauthorized")

    max_bytes = cast(int, request.app.state.telegram_webhook_max_bytes)
    declared = request.headers.get("content-length")
    if declared is not None:
        try:
            declared_size = int(declared)
        except ValueError:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST, detail="Invalid request"
            ) from None
        if declared_size < 0:
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Invalid request")
        if declared_size > max_bytes:
            raise HTTPException(
                status_code=status.HTTP_413_CONTENT_TOO_LARGE,
                detail="Payload too large",
            )

    body = bytearray()
    async for chunk in request.stream():
        if len(body) + len(chunk) > max_bytes:
            raise HTTPException(
                status_code=status.HTTP_413_CONTENT_TOO_LARGE,
                detail="Payload too large",
            )
        body.extend(chunk)
    try:
        raw_payload = json.loads(bytes(body))
        if not isinstance(raw_payload, dict):
            raise TypeError
        payload = cast(dict[str, object], raw_payload)
        bot = cast(Bot, request.app.state.telegram_bot)
        update = Update.model_validate(payload, context={"bot": bot})
    except (json.JSONDecodeError, ValidationError, TypeError):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST, detail="Invalid update"
        ) from None

    inbox = cast(TelegramUpdateInboxService, request.app.state.telegram_update_inbox)
    accepted = await inbox.accept(update.update_id, _telegram_user_id(update), payload)
    if accepted.outcome is TelegramAcceptOutcome.PAYLOAD_MISMATCH:
        logger.warning("telegram_update_payload_mismatch update_id=%s", update.update_id)
    return Response(status_code=status.HTTP_204_NO_CONTENT)
