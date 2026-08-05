"""Application configuration loaded from environment variables."""

import ipaddress
from functools import lru_cache
from typing import Literal

from pydantic import Field, PostgresDsn, SecretStr, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

from app.services.sensitive_content import decode_configured_key


class Settings(BaseSettings):
    """Validated runtime settings shared by the API and bot."""

    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    app_env: Literal["local", "test", "staging", "production"] = "local"
    log_level: str = "INFO"
    database_url: PostgresDsn
    telegram_bot_token: SecretStr
    telegram_webhook_url: str = ""
    telegram_webhook_secret: SecretStr = Field(default=SecretStr(""))
    llm_provider: Literal["stub", "openai"] = "stub"
    openai_api_key: SecretStr = Field(default=SecretStr(""))
    llm_model: str = "stub"
    llm_timeout_seconds: float = Field(default=45, gt=0)
    llm_max_transport_attempts: int = Field(default=2, ge=1, le=5)
    llm_max_repair_attempts: int = Field(default=1, ge=0, le=1)
    llm_prompt_version: str = "analysis_v1"
    content_encryption_key: SecretStr
    raw_content_retention_days: int = Field(default=30, ge=1)
    conversation_min_messages: int = Field(default=4, ge=1)
    conversation_max_characters: int = Field(default=30_000, ge=1)
    conversation_max_participants: int = Field(default=2, ge=2)
    analysis_goal_max_characters: int = Field(default=500, ge=1)
    analysis_price_credits: int = Field(default=1, ge=1)
    payment_provider: str = "mock"
    payment_public_base_url: str = "http://localhost:8000"
    payment_webhook_secret: SecretStr = Field(default=SecretStr("local-mock-secret"))
    payment_currency: str = "RUB"
    payment_webhook_max_age_seconds: int = Field(default=300, gt=0)
    checkout_creation_lease_seconds: int = Field(default=60, gt=0)
    product_analysis_single_price_minor: int = Field(default=19_900, gt=0)
    product_analysis_pack_5_price_minor: int = Field(default=69_900, gt=0)
    product_subscription_monthly_price_minor: int = Field(default=99_000, gt=0)
    product_subscription_monthly_credits: int = Field(default=30, ge=1)
    billing_enabled: bool = False
    billing_kill_switch: bool = False
    yookassa_enabled: bool = False
    stripe_enabled: bool = False
    subscriptions_enabled: bool = False
    refunds_enabled: bool = False
    billing_refund_window_days: int = Field(default=14, ge=1, le=365)
    yookassa_recurring_enabled: bool = False
    yookassa_shop_id: SecretStr = Field(default=SecretStr(""), repr=False)
    yookassa_secret_key: SecretStr = Field(default=SecretStr(""), repr=False)
    yookassa_receipt_email: str = ""
    yookassa_receipts_required: bool = False
    yookassa_vat_code: int = Field(default=1, ge=1, le=6)
    yookassa_webhook_ip_allowlist: str = ""
    yookassa_trusted_proxy_allowlist: str = ""
    billing_trusted_proxies: str = ""
    stripe_secret_key: SecretStr = Field(default=SecretStr(""), repr=False)
    stripe_webhook_secret: SecretStr = Field(default=SecretStr(""), repr=False)
    stripe_portal_url: str = ""
    stripe_price_analysis_single_eur: str = ""
    stripe_price_analysis_single_usd: str = ""
    stripe_price_analysis_pack_5_eur: str = ""
    stripe_price_analysis_pack_5_usd: str = ""
    stripe_amount_analysis_single_eur_minor: int | None = Field(default=None, gt=0)
    stripe_amount_analysis_single_usd_minor: int | None = Field(default=None, gt=0)
    stripe_amount_analysis_pack_5_eur_minor: int | None = Field(default=None, gt=0)
    stripe_amount_analysis_pack_5_usd_minor: int | None = Field(default=None, gt=0)
    stripe_price_subscription_monthly_eur: str = ""
    stripe_price_subscription_monthly_usd: str = ""
    stripe_amount_subscription_monthly_eur_minor: int | None = Field(default=None, gt=0)
    stripe_amount_subscription_monthly_usd_minor: int | None = Field(default=None, gt=0)
    billing_worker_lease_seconds: int = Field(default=60, gt=0)
    billing_worker_max_attempts: int = Field(default=10, ge=1)
    billing_retry_base_seconds: int = Field(default=30, gt=0)
    billing_reconciliation_interval_seconds: int = Field(default=900, gt=0)
    billing_pending_reconciliation_seconds: int = Field(default=900, gt=0)
    payment_webhook_max_bytes: int = Field(default=262_144, gt=0)
    provider_request_timeout_seconds: float = Field(default=15, gt=0)
    subscription_grace_period_days: int = Field(default=3, ge=0)
    billing_consent_version: str = "billing-v1"
    analytics_enabled: bool = False

    @field_validator("payment_currency")
    @classmethod
    def valid_currency(cls, value: str) -> str:
        if len(value) != 3 or not value.isascii() or not value.isalpha() or not value.isupper():
            raise ValueError("currency must be three uppercase ASCII letters")
        return value

    @field_validator("payment_public_base_url")
    @classmethod
    def valid_payment_url(cls, value: str) -> str:
        if not value.startswith(("http://", "https://")):
            raise ValueError("payment public base URL must be HTTP(S)")
        return value.rstrip("/")

    @model_validator(mode="after")
    def validate_production_billing(self) -> "Settings":
        """Fail closed without exposing any secret values in the error."""
        if self.yookassa_recurring_enabled and not self.yookassa_enabled:
            raise ValueError("YooKassa recurring requires YooKassa")
        if self.refunds_enabled and not self.billing_enabled:
            raise ValueError("refunds require billing")
        if self.refunds_enabled and not (self.stripe_enabled or self.yookassa_enabled):
            raise ValueError("refunds require an enabled production payment provider")
        subscription_pairs = (
            (
                self.stripe_price_subscription_monthly_eur,
                self.stripe_amount_subscription_monthly_eur_minor,
            ),
            (
                self.stripe_price_subscription_monthly_usd,
                self.stripe_amount_subscription_monthly_usd_minor,
            ),
        )
        if any(bool(price) != (amount is not None) for price, amount in subscription_pairs):
            raise ValueError(
                "Stripe subscription Price and expected amount must be configured together"
            )
        if self.app_env != "production":
            return self
        encryption_key = self.content_encryption_key.get_secret_value().strip()
        if encryption_key.lower() in {
            "change-me",
            "changeme",
            "development-only-key",
        }:
            raise ValueError("production requires a strong content encryption key")
        try:
            decoded_key = decode_configured_key(encryption_key)
        except ValueError as exc:
            raise ValueError("production content encryption key is malformed") from exc
        if len(decoded_key) < 32 or len(set(decoded_key)) < 8:
            raise ValueError("production requires a strong content encryption key")
        if self.billing_enabled and not self.payment_public_base_url.startswith("https://"):
            raise ValueError("production billing requires an HTTPS public URL")
        if self.billing_enabled and self.payment_provider == "mock":
            raise ValueError("mock payment provider is forbidden in production")
        if (
            self.billing_enabled
            and self.payment_provider != "mock"
            and not (self.yookassa_enabled or self.stripe_enabled)
        ):
            raise ValueError("production billing requires an enabled payment provider")
        if self.yookassa_enabled and not (
            self.yookassa_shop_id.get_secret_value() and self.yookassa_secret_key.get_secret_value()
        ):
            raise ValueError("YooKassa configuration is incomplete")
        if self.yookassa_enabled:
            if (
                self.yookassa_receipts_required
                and not self.content_encryption_key.get_secret_value()
            ):
                raise ValueError("YooKassa receipts require content encryption")
            if not self.yookassa_webhook_ip_allowlist.strip():
                raise ValueError("YooKassa webhook IP allowlist is required")
            for configured in (
                self.yookassa_webhook_ip_allowlist,
                self.yookassa_trusted_proxy_allowlist,
            ):
                try:
                    for value in configured.split(","):
                        if value.strip():
                            ipaddress.ip_network(value.strip(), strict=False)
                except ValueError as exc:
                    raise ValueError("invalid YooKassa network allowlist") from exc
        stripe_key = self.stripe_secret_key.get_secret_value()
        if self.stripe_enabled and not (
            stripe_key and self.stripe_webhook_secret.get_secret_value()
        ):
            raise ValueError("Stripe configuration is incomplete")
        if self.stripe_enabled and not all(
            (
                self.stripe_price_analysis_single_eur,
                self.stripe_price_analysis_single_usd,
                self.stripe_price_analysis_pack_5_eur,
                self.stripe_price_analysis_pack_5_usd,
            )
        ):
            raise ValueError("Stripe one-time Price configuration is incomplete")
        if self.stripe_enabled and not all(
            amount is not None
            for amount in (
                self.stripe_amount_analysis_single_eur_minor,
                self.stripe_amount_analysis_single_usd_minor,
                self.stripe_amount_analysis_pack_5_eur_minor,
                self.stripe_amount_analysis_pack_5_usd_minor,
            )
        ):
            raise ValueError("Stripe one-time expected amounts are incomplete")
        if self.stripe_enabled and stripe_key.startswith(("sk_test_", "rk_test_")):
            raise ValueError("Stripe test credentials are forbidden in production")
        configured_stripe_subscription = any(
            bool(price) and amount is not None for price, amount in subscription_pairs
        )
        if self.subscriptions_enabled and not (
            configured_stripe_subscription or self.yookassa_recurring_enabled
        ):
            raise ValueError("subscriptions require a complete configured offer")
        return self

    def permits_new_checkout(self) -> bool:
        return self.billing_enabled and not self.billing_kill_switch

    def permits_renewal(self) -> bool:
        return self.permits_new_checkout() and self.subscriptions_enabled

    def permits_refund(self) -> bool:
        return self.billing_enabled and self.refunds_enabled and not self.billing_kill_switch

    def permits_webhook_receipt(self) -> bool:
        return self.billing_enabled

    def permits_reconciliation(self) -> bool:
        return self.billing_enabled

    @property
    def webhook_enabled(self) -> bool:
        """Return whether Telegram should be configured for webhook delivery."""
        return bool(self.telegram_webhook_url)


@lru_cache
def get_settings() -> Settings:
    """Return a process-wide immutable settings instance."""
    return Settings()
