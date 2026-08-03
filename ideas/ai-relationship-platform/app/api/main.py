"""FastAPI application factory and ASGI entry point."""

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI
from sqlalchemy.ext.asyncio import AsyncEngine

from app.api.health import router as health_router
from app.api.payments import router as payments_router
from app.api.webhooks import router as webhooks_router
from app.config import Settings, get_settings
from app.db.session import create_engine, create_session_factory
from app.domain.billing import BillingCatalog
from app.domain.products import ProductCatalog
from app.logging import configure_logging
from app.providers.analytics import NoOpAnalyticsClient
from app.providers.payments.base import PaymentProviderName
from app.providers.payments.factory import create_payment_provider
from app.providers.payments.gateway import OneTimePaymentGateway
from app.providers.payments.stripe_gateway import StripeGateway
from app.providers.payments.yookassa_gateway import YooKassaGateway
from app.services.checkout_service import CheckoutService
from app.services.payment_completion_service import PaymentCompletionService
from app.services.payment_service import PaymentService
from app.services.webhook_inbox_service import WebhookInboxService


def create_app(settings: Settings | None = None, engine: AsyncEngine | None = None) -> FastAPI:
    """Build an application with injectable configuration and database engine."""
    resolved_settings = settings or get_settings()
    resolved_engine = engine or create_engine(str(resolved_settings.database_url))
    sessions = create_session_factory(resolved_engine)
    catalog = ProductCatalog(resolved_settings)
    provider = create_payment_provider(resolved_settings)
    gateways: dict[PaymentProviderName, OneTimePaymentGateway] = {}
    if resolved_settings.stripe_enabled:
        gateways[PaymentProviderName.STRIPE] = StripeGateway(
            resolved_settings.stripe_secret_key.get_secret_value(),
            resolved_settings.stripe_webhook_secret.get_secret_value(),
            resolved_settings.provider_request_timeout_seconds,
        )
    if resolved_settings.yookassa_enabled:
        gateways[PaymentProviderName.YOOKASSA] = YooKassaGateway(
            resolved_settings.yookassa_shop_id.get_secret_value(),
            resolved_settings.yookassa_secret_key.get_secret_value(),
            resolved_settings.provider_request_timeout_seconds,
            resolved_settings.yookassa_vat_code,
        )

    @asynccontextmanager
    async def lifespan(_: FastAPI) -> AsyncIterator[None]:
        configure_logging(resolved_settings.log_level)
        yield
        await resolved_engine.dispose()

    application = FastAPI(title="HeartSignal API", version="0.1.0", lifespan=lifespan)
    application.state.db_engine = resolved_engine
    application.state.settings = resolved_settings
    application.state.product_catalog = catalog
    application.state.payment_provider = provider
    application.state.payment_service = PaymentService(
        sessions,
        catalog,
        provider,
        NoOpAnalyticsClient(),
        resolved_settings.payment_provider,
        resolved_settings.checkout_creation_lease_seconds,
    )
    application.state.payment_gateways = gateways
    application.state.checkout_service = CheckoutService(
        sessions, resolved_settings, BillingCatalog(resolved_settings), gateways
    )
    application.state.payment_completion_service = PaymentCompletionService(
        sessions, resolved_settings.app_env == "production"
    )
    application.state.webhook_inbox = WebhookInboxService(sessions)
    application.include_router(health_router)
    application.include_router(payments_router)
    application.include_router(webhooks_router)
    return application


app = create_app()
