"""Application configuration loaded from environment variables."""

from functools import lru_cache
from typing import Literal

from pydantic import Field, PostgresDsn, SecretStr, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


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

    @property
    def webhook_enabled(self) -> bool:
        """Return whether Telegram should be configured for webhook delivery."""
        return bool(self.telegram_webhook_url)


@lru_cache
def get_settings() -> Settings:
    """Return a process-wide immutable settings instance."""
    return Settings()
