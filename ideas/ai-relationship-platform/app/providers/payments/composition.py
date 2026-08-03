"""Shared payment composition for API, bot, and background workers."""

from dataclasses import dataclass

from app.config import Settings
from app.providers.payments.base import PaymentProvider, PaymentProviderName
from app.providers.payments.gateway import OneTimePaymentGateway
from app.providers.payments.mock import MockPaymentProvider
from app.providers.payments.stripe_gateway import StripeGateway
from app.providers.payments.yookassa_gateway import YooKassaGateway


@dataclass(frozen=True)
class PaymentComponents:
    legacy: PaymentProvider | None
    gateways: dict[PaymentProviderName, OneTimePaymentGateway]


def create_payment_components(settings: Settings) -> PaymentComponents:
    """Build only enabled adapters; production never constructs the legacy mock."""
    legacy: PaymentProvider | None = None
    if settings.payment_provider == "mock":
        if settings.app_env == "production" and settings.billing_enabled:
            raise ValueError("mock payment provider is forbidden in production")
        legacy = MockPaymentProvider(
            settings.payment_public_base_url,
            settings.payment_webhook_secret.get_secret_value(),
            settings.payment_webhook_max_age_seconds,
        )
    gateways: dict[PaymentProviderName, OneTimePaymentGateway] = {}
    if settings.stripe_enabled:
        gateways[PaymentProviderName.STRIPE] = StripeGateway(
            settings.stripe_secret_key.get_secret_value(),
            settings.stripe_webhook_secret.get_secret_value(),
            settings.provider_request_timeout_seconds,
        )
    if settings.yookassa_enabled:
        gateways[PaymentProviderName.YOOKASSA] = YooKassaGateway(
            settings.yookassa_shop_id.get_secret_value(),
            settings.yookassa_secret_key.get_secret_value(),
            settings.provider_request_timeout_seconds,
            settings.yookassa_vat_code,
        )
    return PaymentComponents(legacy, gateways)
