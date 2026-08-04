"""Production runtime policy and bounded maintenance settings."""

import re
from functools import lru_cache
from urllib.parse import urlsplit

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict

from app.config import Settings

_TELEGRAM_WEBHOOK_PATH = "/telegram/webhook"
_TELEGRAM_SECRET = re.compile(r"[A-Za-z0-9_-]{1,256}\Z")


class DeploymentSettings(BaseSettings):
    """Operational settings kept separate from product and billing configuration."""

    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    telegram_webhook_max_bytes: int = Field(default=1_048_576, gt=0, le=10_485_760)
    analysis_processing_stale_seconds: int = Field(default=900, ge=60)
    maintenance_interval_seconds: float = Field(default=300, gt=0)
    maintenance_batch_size: int = Field(default=100, ge=1, le=10_000)


def validate_telegram_webhook(settings: Settings) -> None:
    """Fail closed for webhook deployments without exposing secret values."""
    if not settings.webhook_enabled:
        return
    parsed = urlsplit(settings.telegram_webhook_url)
    if parsed.path != _TELEGRAM_WEBHOOK_PATH or parsed.query or parsed.fragment:
        raise ValueError(f"Telegram webhook URL must end with {_TELEGRAM_WEBHOOK_PATH}")
    secret = settings.telegram_webhook_secret.get_secret_value()
    if _TELEGRAM_SECRET.fullmatch(secret) is None:
        raise ValueError("Telegram webhook secret must use 1-256 safe ASCII characters")
    if settings.app_env == "production":
        if parsed.scheme != "https" or not parsed.netloc:
            raise ValueError("production Telegram webhook requires a public HTTPS URL")
        if len(secret) < 32:
            raise ValueError("production Telegram webhook requires a strong secret")


@lru_cache
def get_deployment_settings() -> DeploymentSettings:
    return DeploymentSettings()
