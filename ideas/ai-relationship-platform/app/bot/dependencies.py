"""Per-update database dependency injection."""

from collections.abc import Awaitable, Callable
from typing import Any

from aiogram import BaseMiddleware
from aiogram.types import TelegramObject
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.providers.analytics import AnalyticsClient
from app.repositories.users import SqlAlchemyUserRepository
from app.services.onboarding import OnboardingService


class OnboardingDependencyMiddleware(BaseMiddleware):
    """Own a session per update; no handler uses a global connection."""

    def __init__(
        self,
        sessions: async_sessionmaker[AsyncSession],
        analytics: AnalyticsClient,
    ) -> None:
        self._sessions = sessions
        self._analytics = analytics

    async def __call__(
        self,
        handler: Callable[[TelegramObject, dict[str, Any]], Awaitable[Any]],
        event: TelegramObject,
        data: dict[str, Any],
    ) -> Any:
        async with self._sessions() as session:
            data["onboarding"] = OnboardingService(
                SqlAlchemyUserRepository(session), self._analytics
            )
            return await handler(event, data)
