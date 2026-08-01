"""Shared test fixtures."""

import pytest
from pydantic import SecretStr

from app.config import Settings


@pytest.fixture
def settings() -> Settings:
    return Settings(
        app_env="test",
        database_url="postgresql+asyncpg://user:pass@db:5432/test",
        telegram_bot_token=SecretStr("123456789:AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA"),
        content_encryption_key=SecretStr("test-only-key"),
    )
