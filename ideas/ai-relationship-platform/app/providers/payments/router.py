"""Select configured adapters without making provider calls."""

from app.config import Settings
from app.providers.payments.base import PaymentProvider, PaymentProviderError, PaymentProviderName


class PaymentProviderRouter:
    def __init__(self, settings: Settings, providers: dict[PaymentProviderName, PaymentProvider]):
        self._settings = settings
        self._providers = providers

    def get(self, provider: PaymentProviderName) -> PaymentProvider:
        if self._settings.app_env == "production" and provider is PaymentProviderName.MOCK:
            raise PaymentProviderError("mock payment provider is forbidden in production")
        try:
            return self._providers[provider]
        except KeyError as exc:
            raise PaymentProviderError("payment provider is not configured") from exc
