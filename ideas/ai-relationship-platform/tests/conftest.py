"""Shared test fixtures."""

import os

import pytest
from pydantic import SecretStr

from app.config import Settings

pytest_plugins = ("tests.payment_postgres_helpers",)

# Application modules expose an ASGI entry point at import time. Provide isolated,
# non-production values so test collection never depends on a developer's .env file.
os.environ.update(
    {
        "APP_ENV": "test",
        "DATABASE_URL": "postgresql+asyncpg://user:pass@db:5432/test",
        "TELEGRAM_BOT_TOKEN": "123456789:AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA",
        "CONTENT_ENCRYPTION_KEY": "test-only-key",
    }
)


@pytest.fixture
def settings() -> Settings:
    return Settings(
        app_env="test",
        database_url="postgresql+asyncpg://user:pass@db:5432/test",
        telegram_bot_token=SecretStr("123456789:AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA"),
        content_encryption_key=SecretStr("test-only-key"),
    )
