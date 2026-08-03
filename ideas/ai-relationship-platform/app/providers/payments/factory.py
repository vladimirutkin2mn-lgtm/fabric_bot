from app.config import Settings
from app.providers.payments.base import PaymentProvider, PaymentProviderError
from app.providers.payments.mock import MockPaymentProvider


def create_payment_provider(settings: Settings) -> PaymentProvider:
    if settings.payment_provider != "mock":
        raise PaymentProviderError("unsupported payment provider")
    return MockPaymentProvider(
        settings.payment_public_base_url,
        settings.payment_webhook_secret.get_secret_value(),
        settings.payment_webhook_max_age_seconds,
    )
