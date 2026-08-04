"""Configuration isolated to analytics, admin metrics and error reporting."""

from functools import lru_cache
from typing import Literal

from pydantic import SecretStr, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class ObservabilitySettings(BaseSettings):
    """Validated observability settings loaded from the same environment file."""

    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    app_env: Literal["local", "test", "staging", "production"] = "local"
    analytics_backend: Literal["noop", "postgres"] = "noop"
    error_reporting_backend: Literal["noop", "logging"] = "logging"
    admin_metrics_enabled: bool = False
    admin_api_token: SecretStr = SecretStr("")

    @model_validator(mode="after")
    def validate_admin_auth(self) -> "ObservabilitySettings":
        if not self.admin_metrics_enabled:
            return self
        token = self.admin_api_token.get_secret_value().strip()
        if not token:
            raise ValueError("enabled admin metrics require a token")
        if self.app_env == "production" and (
            len(token) < 32 or token.lower() in {"change-me", "changeme", "development-only-token"}
        ):
            raise ValueError("production admin metrics require a strong token")
        return self


@lru_cache
def get_observability_settings() -> ObservabilitySettings:
    return ObservabilitySettings()
