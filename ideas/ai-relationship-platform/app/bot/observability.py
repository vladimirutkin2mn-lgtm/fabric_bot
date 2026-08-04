"""Correlation and safe unexpected-error reporting for Telegram updates."""

from collections.abc import Awaitable, Callable
from typing import Any
from uuid import uuid4

from aiogram import BaseMiddleware
from aiogram.types import TelegramObject

from app.observability.context import reset_correlation_id, set_correlation_id
from app.observability.errors import ErrorReporter, report_unexpected


class TelegramObservabilityMiddleware(BaseMiddleware):
    """Use update identity only; never include Telegram user or chat identity."""

    def __init__(self, reporter: ErrorReporter) -> None:
        self._reporter = reporter

    async def __call__(
        self,
        handler: Callable[[TelegramObject, dict[str, Any]], Awaitable[Any]],
        event: TelegramObject,
        data: dict[str, Any],
    ) -> Any:
        update_id = getattr(event, "update_id", None)
        proposed = f"tg-update-{update_id}" if isinstance(update_id, int) else uuid4().hex
        _, token = set_correlation_id(proposed)
        try:
            return await handler(event, data)
        except Exception as error:
            report_unexpected(
                self._reporter,
                error,
                surface="telegram",
                operation=type(event).__name__,
            )
            raise
        finally:
            reset_correlation_id(token)
