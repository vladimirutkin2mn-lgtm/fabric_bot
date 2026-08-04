"""FastAPI application factory and ASGI entry point."""

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from aiogram import Bot
from fastapi import FastAPI
from sqlalchemy.ext.asyncio import AsyncEngine

from app.api.admin import router as admin_router
from app.api.health import router as health_router
from app.api.payments import router as payments_router
from app.api.telegram import router as telegram_router
from app.api.webhooks import router as webhooks_router
from app.bot.main import configure_webhook
from app.config import Settings, get_settings
from app.db.session import create_engine, create_session_factory
from app.deployment import (
    DeploymentSettings,
    get_deployment_settings,
    validate_telegram_webhook,
)
from app.domain.billing import BillingCatalog
from app.domain.products import ProductCatalog
from app.logging import configure_logging
from app.observability.errors import LoggingErrorReporter, NoOpErrorReporter
from app.observability.http import HttpObservabilityMiddleware
from app.observability.settings import ObservabilitySettings, get_observability_settings
from app.providers.analytics_postgres import create_analytics_client
from app.providers.payments.composition import create_payment_components
from app.services.admin_metrics import AdminMetricsService
from app.services.checkout_service import CheckoutService
from app.services.payment_completion_service import PaymentCompletionService
from app.services.payment_service import PaymentService
from app.services.sensitive_content import AESGCMSensitiveContentCipher, decode_configured_key
from app.services.telegram_update_inbox import TelegramUpdateInboxService
from app.services.webhook_inbox_service import WebhookInboxService


def create_app(
    settings: Settings | None = None,
    engine: AsyncEngine | None = None,
    observability_settings: ObservabilitySettings | None = None,
    deployment_settings: DeploymentSettings | None = None,
    telegram_bot: Bot | None = None,
    register_telegram_webhook: bool = True,
) -> FastAPI:
    """Build an application with injectable configuration and database engine."""
    resolved_settings = settings or get_settings()
    resolved_observability = observability_settings or get_observability_settings()
    resolved_deployment = deployment_settings or get_deployment_settings()
    resolved_engine = engine or create_engine(str(resolved_settings.database_url))
    sessions = create_session_factory(resolved_engine)
    catalog = ProductCatalog(resolved_settings)
    payments = create_payment_components(resolved_settings)
    analytics = create_analytics_client(sessions, resolved_observability)
    reporter = (
        LoggingErrorReporter()
        if resolved_observability.error_reporting_backend == "logging"
        else NoOpErrorReporter()
    )

    resolved_bot: Bot | None = None
    telegram_inbox: TelegramUpdateInboxService | None = None
    owns_bot = False
    if resolved_settings.webhook_enabled:
        validate_telegram_webhook(resolved_settings)
        resolved_bot = telegram_bot or Bot(
            token=resolved_settings.telegram_bot_token.get_secret_value()
        )
        owns_bot = telegram_bot is None
        cipher = AESGCMSensitiveContentCipher(
            decode_configured_key(resolved_settings.content_encryption_key.get_secret_value())
        )
        telegram_inbox = TelegramUpdateInboxService(
            sessions,
            cipher,
            lease_seconds=resolved_deployment.telegram_update_lease_seconds,
            retry_base_seconds=resolved_deployment.telegram_update_retry_base_seconds,
            max_attempts=resolved_deployment.telegram_update_max_attempts,
        )

    @asynccontextmanager
    async def lifespan(_: FastAPI) -> AsyncIterator[None]:
        configure_logging(resolved_settings.log_level)
        try:
            if (
                register_telegram_webhook
                and resolved_bot is not None
                and resolved_settings.webhook_enabled
            ):
                await configure_webhook(resolved_bot, resolved_settings)
            yield
        finally:
            if owns_bot and resolved_bot is not None:
                await resolved_bot.session.close()
            await resolved_engine.dispose()

    application = FastAPI(title="HeartSignal API", version="0.1.0", lifespan=lifespan)
    application.add_middleware(HttpObservabilityMiddleware, reporter=reporter)
    application.state.db_engine = resolved_engine
    application.state.settings = resolved_settings
    application.state.observability_settings = resolved_observability
    application.state.deployment_settings = resolved_deployment
    application.state.analytics = analytics
    application.state.error_reporter = reporter
    application.state.admin_metrics_service = AdminMetricsService(sessions)
    application.state.product_catalog = catalog
    application.state.payment_provider = payments.legacy
    application.state.payment_service = (
        PaymentService(
            sessions,
            catalog,
            payments.legacy,
            analytics,
            resolved_settings.payment_provider,
            resolved_settings.checkout_creation_lease_seconds,
        )
        if payments.legacy is not None
        else None
    )
    application.state.payment_gateways = payments.gateways
    application.state.checkout_service = CheckoutService(
        sessions, resolved_settings, BillingCatalog(resolved_settings), payments.gateways
    )
    application.state.payment_completion_service = PaymentCompletionService(
        sessions, resolved_settings.app_env == "production"
    )
    application.state.webhook_inbox = WebhookInboxService(sessions)
    application.include_router(health_router)
    application.include_router(admin_router)
    application.include_router(payments_router)
    application.include_router(webhooks_router)
    if resolved_bot is not None and telegram_inbox is not None:
        application.state.telegram_bot = resolved_bot
        application.state.telegram_update_inbox = telegram_inbox
        application.state.telegram_webhook_max_bytes = (
            resolved_deployment.telegram_webhook_max_bytes
        )
        application.include_router(telegram_router)
    return application


app = create_app()
