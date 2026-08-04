"""Deployment policy, managed database URL, and release-command tests."""

import pytest
from pydantic import SecretStr, ValidationError

from app.cli.release import asyncpg_dsn
from app.config import Settings
from app.db.session import normalize_async_database_url
from app.deployment import DeploymentSettings, validate_telegram_webhook


def test_managed_postgres_urls_select_asyncpg() -> None:
    assert normalize_async_database_url("postgres://u:p@db/name") == (
        "postgresql+asyncpg://u:p@db/name"
    )
    assert normalize_async_database_url("postgresql://u:p@db/name") == (
        "postgresql+asyncpg://u:p@db/name"
    )
    assert normalize_async_database_url("postgresql+asyncpg://u:p@db/name") == (
        "postgresql+asyncpg://u:p@db/name"
    )
    assert asyncpg_dsn("postgres://u:p@db/name") == "postgresql://u:p@db/name"
    assert asyncpg_dsn("postgresql+asyncpg://u:p@db/name") == "postgresql://u:p@db/name"
    assert asyncpg_dsn("postgresql://u:p@db/name") == "postgresql://u:p@db/name"


def test_webhook_policy_accepts_local_and_strong_production_urls(settings: Settings) -> None:
    local = settings.model_copy(
        update={
            "telegram_webhook_url": "http://localhost:8000/telegram/webhook",
            "telegram_webhook_secret": SecretStr("local-secret"),
        }
    )
    validate_telegram_webhook(local)

    production = settings.model_copy(
        update={
            "app_env": "production",
            "telegram_webhook_url": "https://example.com/telegram/webhook",
            "telegram_webhook_secret": SecretStr("a" * 32),
        }
    )
    validate_telegram_webhook(production)


@pytest.mark.parametrize(
    ("url", "secret"),
    [
        ("https://example.com/wrong", "a" * 32),
        ("https://example.com/telegram/webhook?token=x", "a" * 32),
        ("http://example.com/telegram/webhook", "a" * 32),
        ("https://example.com/telegram/webhook", "short"),
        ("https://example.com/telegram/webhook", "unsafe secret value"),
    ],
)
def test_production_webhook_policy_fails_closed(settings: Settings, url: str, secret: str) -> None:
    configured = settings.model_copy(
        update={
            "app_env": "production",
            "telegram_webhook_url": url,
            "telegram_webhook_secret": SecretStr(secret),
        }
    )
    with pytest.raises(ValueError):
        validate_telegram_webhook(configured)


def test_deployment_settings_reject_unbounded_values() -> None:
    with pytest.raises(ValidationError):
        DeploymentSettings(telegram_webhook_max_bytes=0)
    with pytest.raises(ValidationError):
        DeploymentSettings(telegram_update_lease_seconds=29)
    with pytest.raises(ValidationError):
        DeploymentSettings(telegram_update_retry_base_seconds=0)
    with pytest.raises(ValidationError):
        DeploymentSettings(telegram_update_max_attempts=101)
    with pytest.raises(ValidationError):
        DeploymentSettings(telegram_worker_idle_seconds=0)
    with pytest.raises(ValidationError):
        DeploymentSettings(analysis_processing_stale_seconds=59)
    with pytest.raises(ValidationError):
        DeploymentSettings(maintenance_batch_size=10_001)
