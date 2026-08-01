"""Replaceable per-user fixed-window rate limiter middleware."""

import time
from collections import defaultdict, deque
from collections.abc import Awaitable, Callable
from typing import Any

from aiogram import BaseMiddleware
from aiogram.types import CallbackQuery, Message, TelegramObject

from app.bot import texts


class FixedWindowRateLimiter:
    def __init__(self, limit: int = 5, window_seconds: float = 10.0) -> None:
        self._limit = limit
        self._window = window_seconds
        self._requests: dict[int, deque[float]] = defaultdict(deque)

    def allow(self, user_id: int, now: float | None = None) -> bool:
        current = time.monotonic() if now is None else now
        requests = self._requests[user_id]
        while requests and requests[0] <= current - self._window:
            requests.popleft()
        if len(requests) >= self._limit:
            return False
        requests.append(current)
        return True


class RateLimitMiddleware(BaseMiddleware):
    """Limit /start messages and all callbacks without inspecting message text content."""

    def __init__(self, limiter: FixedWindowRateLimiter) -> None:
        self._limiter = limiter

    async def __call__(
        self,
        handler: Callable[[TelegramObject, dict[str, Any]], Awaitable[Any]],
        event: TelegramObject,
        data: dict[str, Any],
    ) -> Any:
        user_id: int | None = None
        if isinstance(event, CallbackQuery):
            user_id = event.from_user.id
        elif isinstance(event, Message) and event.text and event.text.split()[0] == "/start":
            user_id = event.from_user.id if event.from_user else None
        if user_id is not None and not self._limiter.allow(user_id):
            if isinstance(event, CallbackQuery):
                await event.answer(texts.RATE_LIMITED, show_alert=True)
            elif isinstance(event, Message):
                await event.answer(texts.RATE_LIMITED)
            return None
        return await handler(event, data)
