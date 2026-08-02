"""Per-update database dependency injection."""

from collections.abc import Awaitable, Callable
from typing import Any

from aiogram import BaseMiddleware
from aiogram.types import TelegramObject
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.config import Settings
from app.providers.analytics import AnalyticsClient
from app.repositories.analyses import SqlAlchemyAnalysisRepository
from app.repositories.users import SqlAlchemyUserRepository
from app.services.conversation_intake import ConversationIntakeService
from app.services.conversation_parser import ConversationParser
from app.services.onboarding import OnboardingService


class OnboardingDependencyMiddleware(BaseMiddleware):
    """Own a session per update; no handler uses a global connection."""

    def __init__(
        self,
        sessions: async_sessionmaker[AsyncSession],
        analytics: AnalyticsClient,
        settings: Settings,
    ) -> None:
        self._sessions = sessions
        self._analytics = analytics
        self._settings = settings

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
            data["intake"] = ConversationIntakeService(
                SqlAlchemyAnalysisRepository(session),
                ConversationParser(
                    self._settings.conversation_min_messages,
                    self._settings.conversation_max_characters,
                    self._settings.conversation_max_participants,
                ),
                self._analytics,
                self._settings.analysis_goal_max_characters,
            )
            return await handler(event, data)
