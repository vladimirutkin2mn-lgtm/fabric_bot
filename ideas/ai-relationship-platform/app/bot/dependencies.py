"""Per-update database dependency injection."""

from collections.abc import Awaitable, Callable
from typing import Any

from aiogram import BaseMiddleware
from aiogram.types import TelegramObject
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.config import Settings
from app.providers.analytics import AnalyticsClient
from app.providers.llm.base import LLMClient
from app.repositories.analyses import SqlAlchemyAnalysisRepository
from app.repositories.users import SqlAlchemyUserRepository
from app.services.analysis_service import create_analysis_service
from app.services.conversation_intake import ConversationIntakeService
from app.services.conversation_parser import ConversationParser
from app.services.onboarding import OnboardingService
from app.services.report_renderer import ReportRenderer
from app.services.report_service import ReportService


class OnboardingDependencyMiddleware(BaseMiddleware):
    """Own a session per update; no handler uses a global connection."""

    def __init__(
        self,
        sessions: async_sessionmaker[AsyncSession],
        analytics: AnalyticsClient,
        settings: Settings,
        llm: LLMClient,
    ) -> None:
        self._sessions = sessions
        self._analytics = analytics
        self._settings = settings
        self._llm = llm

    async def __call__(
        self,
        handler: Callable[[TelegramObject, dict[str, Any]], Awaitable[Any]],
        event: TelegramObject,
        data: dict[str, Any],
    ) -> Any:
        async with self._sessions() as session:
            analyses = SqlAlchemyAnalysisRepository(session)
            data["onboarding"] = OnboardingService(
                SqlAlchemyUserRepository(session), self._analytics
            )
            data["intake"] = ConversationIntakeService(
                analyses,
                ConversationParser(
                    self._settings.conversation_min_messages,
                    self._settings.conversation_max_characters,
                    self._settings.conversation_max_participants,
                ),
                self._analytics,
                self._settings.analysis_goal_max_characters,
            )
            data["analysis_service"] = create_analysis_service(
                self._settings, analyses, self._llm, self._analytics
            )
            data["reports"] = ReportService(analyses, ReportRenderer())
            data["analysis_repository"] = analyses
            data["analytics"] = self._analytics
            return await handler(event, data)
