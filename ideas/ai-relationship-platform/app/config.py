"""Application configuration loaded from environment variables."""

from functools import lru_cache
from typing import Literal

from pydantic import Field, PostgresDsn, SecretStr
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

    @property
    def webhook_enabled(self) -> bool:
        """Return whether Telegram should be configured for webhook delivery."""
        return bool(self.telegram_webhook_url)


@lru_cache
def get_settings() -> Settings:
    """Return a process-wide immutable settings instance."""
    return Settings()
