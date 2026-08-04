"""Authenticated Telegram webhook transport."""

import hmac
import json
from typing import Annotated, cast

from aiogram import Bot, Dispatcher
from aiogram.types import Update
from fastapi import APIRouter, Header, HTTPException, Request, Response, status
from pydantic import ValidationError

from app.config import Settings

router = APIRouter(tags=["telegram"])


@router.post("/telegram/webhook", status_code=status.HTTP_204_NO_CONTENT)
async def telegram_webhook(
    request: Request,
    telegram_secret: Annotated[str | None, Header(alias="X-Telegram-Bot-Api-Secret-Token")] = None,
) -> Response:
    """Validate Telegram's secret header and feed one bounded update to aiogram."""
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
                status_code=status.HTTP_413_REQUEST_ENTITY_TOO_LARGE,
                detail="Payload too large",
            )

    body = bytearray()
    async for chunk in request.stream():
        if len(body) + len(chunk) > max_bytes:
            raise HTTPException(
                status_code=status.HTTP_413_REQUEST_ENTITY_TOO_LARGE,
                detail="Payload too large",
            )
        body.extend(chunk)
    try:
        payload = json.loads(bytes(body))
        bot = cast(Bot, request.app.state.telegram_bot)
        update = Update.model_validate(payload, context={"bot": bot})
    except (json.JSONDecodeError, ValidationError, TypeError):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST, detail="Invalid update"
        ) from None

    dispatcher = cast(Dispatcher, request.app.state.telegram_dispatcher)
    await dispatcher.feed_update(bot, update)
    return Response(status_code=status.HTTP_204_NO_CONTENT)
