"""aiogram bootstrap for local long-polling and shared dispatcher construction."""

import asyncio
import logging

from aiogram import Bot, Dispatcher
from sqlalchemy.ext.asyncio import AsyncEngine

from app.bot.dependencies import OnboardingDependencyMiddleware
from app.bot.handlers import router
from app.bot.observability import TelegramObservabilityMiddleware
from app.bot.postgres_fsm import PostgresEventIsolation, PostgresFSMStorage
from app.bot.rate_limit import FixedWindowRateLimiter, RateLimitMiddleware
from app.bot.refund_handlers import router as refund_router
from app.bot.subscription_handlers import router as subscription_router
from app.config import Settings, get_settings
from app.db.session import create_engine, create_session_factory
from app.domain.billing import BillingCatalog
from app.domain.products import ProductCatalog
from app.logging import configure_logging
from app.observability.errors import LoggingErrorReporter, NoOpErrorReporter
from app.observability.settings import ObservabilitySettings, get_observability_settings
from app.providers.analytics_postgres import create_analytics_client
from app.providers.llm.base import close_llm_client
from app.providers.llm.factory import create_llm_client
from app.providers.payments.composition import create_payment_components
from app.services.checkout_service import CheckoutService
from app.services.refund_service import RefundService
from app.services.sensitive_content import AESGCMSensitiveContentCipher, decode_configured_key
from app.services.subscription_checkout_service import SubscriptionCheckoutService
from app.services.subscription_event_processor import SubscriptionEventProcessor
from app.services.subscription_lifecycle import SubscriptionLifecycleService
from app.services.subscription_management_service import SubscriptionManagementService

logger = logging.getLogger(__name__)


def create_dispatcher(
    settings: Settings,
    observability_settings: ObservabilitySettings | None = None,
    engine: AsyncEngine | None = None,
) -> Dispatcher:
    """Create a dispatcher with durable FSM and explicit per-update dependencies."""
    resolved_observability = observability_settings or get_observability_settings()
    resolved_engine = engine or create_engine(str(settings.database_url))
    sessions = create_session_factory(resolved_engine)
    cipher = AESGCMSensitiveContentCipher(
        decode_configured_key(settings.content_encryption_key.get_secret_value())
    )
    dispatcher = Dispatcher(
        storage=PostgresFSMStorage(sessions, cipher),
        events_isolation=PostgresEventIsolation(resolved_engine),
    )
    llm = create_llm_client(settings)
    payments = create_payment_components(settings)
    product_catalog = ProductCatalog(settings)
    billing_catalog = BillingCatalog(settings)
    analytics = create_analytics_client(sessions, resolved_observability)
    reporter = (
        LoggingErrorReporter()
        if resolved_observability.error_reporting_backend == "logging"
        else NoOpErrorReporter()
    )
    lifecycle = SubscriptionLifecycleService(sessions)
    processor = SubscriptionEventProcessor(
        sessions, lifecycle, settings.subscription_grace_period_days
    )
    subscription_gateways = {
        name.value: gateway for name, gateway in payments.subscription_gateways.items()
    }
    refund_gateways = {name.value: gateway for name, gateway in payments.refund_gateways.items()}
    dependency_middleware = OnboardingDependencyMiddleware(
        sessions,
        analytics,
        settings,
        llm,
        payments.legacy,
        product_catalog,
        CheckoutService(sessions, settings, billing_catalog, payments.gateways),
        SubscriptionCheckoutService(
            sessions, settings, billing_catalog, payments.subscription_gateways
        ),
        SubscriptionManagementService(sessions, settings, subscription_gateways, processor),
        RefundService(sessions, settings, refund_gateways),
    )
    rate_middleware = RateLimitMiddleware(FixedWindowRateLimiter())
    dispatcher.message.outer_middleware(rate_middleware)
    dispatcher.callback_query.outer_middleware(rate_middleware)
    dispatcher.update.outer_middleware(TelegramObservabilityMiddleware(reporter))
    dispatcher.update.outer_middleware(dependency_middleware)
    dispatcher.include_router(refund_router)
    dispatcher.include_router(subscription_router)
    dispatcher.include_router(router)
    dispatcher["database_engine"] = resolved_engine
    dispatcher["owns_database_engine"] = engine is None
    dispatcher["llm_client"] = llm
    dispatcher["analytics"] = analytics
    dispatcher["error_reporter"] = reporter
    return dispatcher


async def close_dispatcher(dispatcher: Dispatcher) -> None:
    """Close provider/FSM resources and only engines owned by this dispatcher."""
    try:
        await close_llm_client(dispatcher["llm_client"])
    except Exception:
        logger.warning("LLM client shutdown failed")
    await dispatcher.fsm.close()
    if dispatcher["owns_database_engine"]:
        await dispatcher["database_engine"].dispose()


async def configure_webhook(bot: Bot, settings: Settings) -> None:
    """Register webhook delivery using Telegram's verification secret."""
    await bot.set_webhook(
        url=settings.telegram_webhook_url,
        secret_token=settings.telegram_webhook_secret.get_secret_value(),
        allowed_updates=["message", "callback_query"],
    )


async def run(settings: Settings | None = None) -> None:
    """Run local polling; production webhook updates belong to the durable worker."""
    resolved_settings = settings or get_settings()
    if resolved_settings.webhook_enabled:
        raise ValueError("webhook mode requires app.workers.telegram")
    configure_logging(resolved_settings.log_level)
    bot = Bot(token=resolved_settings.telegram_bot_token.get_secret_value())
    dispatcher = create_dispatcher(resolved_settings)
    try:
        await bot.delete_webhook(drop_pending_updates=False)
        await dispatcher.start_polling(bot)
    finally:
        try:
            await close_dispatcher(dispatcher)
        finally:
            await bot.session.close()


def main() -> None:
    """Synchronous console entry point."""
    asyncio.run(run())


if __name__ == "__main__":
    main()
