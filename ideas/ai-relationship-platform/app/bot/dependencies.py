"""Per-update database dependency injection."""

from collections.abc import Awaitable, Callable
from typing import Any

from aiogram import BaseMiddleware
from aiogram.types import TelegramObject
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.config import Settings
from app.domain.products import ProductCatalog
from app.providers.analytics import AnalyticsClient
from app.providers.llm.base import LLMClient
from app.providers.payments.base import PaymentProvider
from app.repositories.analyses import SqlAlchemyAnalysisRepository
from app.repositories.users import SqlAlchemyUserRepository
from app.services.analysis_service import create_analysis_service
from app.services.checkout_service import CheckoutService
from app.services.conversation_intake import ConversationIntakeService
from app.services.conversation_parser import ConversationParser
from app.services.credits_service import CreditsService
from app.services.data_deletion import DataDeletionService
from app.services.monetized_analysis import MonetizedAnalysisService
from app.services.onboarding import OnboardingService
from app.services.payment_service import PaymentService
from app.services.preview_entitlement import PreviewEntitlementService
from app.services.report_renderer import ReportRenderer
from app.services.report_service import ReportService
from app.services.sensitive_content import AESGCMSensitiveContentCipher, decode_configured_key
from app.services.subscription_checkout_service import SubscriptionCheckoutService
from app.services.subscription_management_service import SubscriptionManagementService


class OnboardingDependencyMiddleware(BaseMiddleware):
    """Own a session per update; no handler uses a global connection."""

    def __init__(
        self,
        sessions: async_sessionmaker[AsyncSession],
        analytics: AnalyticsClient,
        settings: Settings,
        llm: LLMClient,
        payment_provider: PaymentProvider | None,
        product_catalog: ProductCatalog,
        checkout_service: CheckoutService,
        subscription_checkout: SubscriptionCheckoutService | None = None,
        subscriptions: SubscriptionManagementService | None = None,
    ) -> None:
        self._sessions = sessions
        self._analytics = analytics
        self._settings = settings
        self._llm = llm
        self._payment_provider, self._product_catalog = payment_provider, product_catalog
        self._checkout_service = checkout_service
        self._subscription_checkout = subscription_checkout
        self._subscriptions = subscriptions

    async def __call__(
        self,
        handler: Callable[[TelegramObject, dict[str, Any]], Awaitable[Any]],
        event: TelegramObject,
        data: dict[str, Any],
    ) -> Any:
        async with self._sessions() as session:
            cipher = AESGCMSensitiveContentCipher(
                decode_configured_key(self._settings.content_encryption_key.get_secret_value())
            )
            analyses = SqlAlchemyAnalysisRepository(
                session, cipher, self._settings.raw_content_retention_days
            )
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
            analysis_service = create_analysis_service(
                self._settings, analyses, self._llm, self._analytics
            )
            data["analysis_service"] = analysis_service
            credits = CreditsService(self._sessions)
            previews = PreviewEntitlementService(self._sessions)
            data["credits"] = credits
            data["previews"] = previews
            data["catalog"] = self._product_catalog
            data["payments"] = (
                PaymentService(
                    self._sessions,
                    self._product_catalog,
                    self._payment_provider,
                    self._analytics,
                    self._settings.payment_provider,
                    self._settings.checkout_creation_lease_seconds,
                )
                if self._payment_provider is not None
                else None
            )
            data["checkout"] = self._checkout_service
            data["subscription_checkout"] = self._subscription_checkout
            data["subscriptions"] = self._subscriptions
            data["billing_settings"] = self._settings
            data["monetized"] = MonetizedAnalysisService(
                self._sessions,
                credits,
                previews,
                analysis_service,
                self._settings.analysis_price_credits,
                self._analytics,
            )
            data["analysis_price"] = self._settings.analysis_price_credits
            deletion = DataDeletionService(session, self._analytics)
            data["reports"] = ReportService(analyses, ReportRenderer(), self._analytics, deletion)
            data["analysis_repository"] = analyses
            data["analytics"] = self._analytics
            data["data_deletion"] = deletion
            data["privacy_retention_days"] = self._settings.raw_content_retention_days
            return await handler(event, data)
