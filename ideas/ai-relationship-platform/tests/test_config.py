"""Configuration tests."""

from pydantic import SecretStr
from pytest import MonkeyPatch

from app.config import Settings


def test_empty_webhook_uses_polling(settings: Settings) -> None:
    assert settings.webhook_enabled is False


def test_webhook_url_enables_webhook(settings: Settings) -> None:
    configured = settings.model_copy(update={"telegram_webhook_url": "https://example.com/hook"})
    assert configured.webhook_enabled is True


def test_settings_are_loaded_from_environment(monkeypatch: MonkeyPatch) -> None:
    monkeypatch.setenv("APP_ENV", "staging")
    monkeypatch.setenv("LOG_LEVEL", "DEBUG")
    monkeypatch.setenv("DATABASE_URL", "postgresql+asyncpg://env:pass@db:5432/envdb")
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "987654321:BBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBB")
    monkeypatch.setenv("CONTENT_ENCRYPTION_KEY", "environment-secret")
    monkeypatch.setenv("RAW_CONTENT_RETENTION_DAYS", "45")

    configured = Settings(_env_file=None)  # type: ignore[call-arg]

    assert configured.app_env == "staging"
    assert configured.log_level == "DEBUG"
    assert str(configured.database_url) == "postgresql+asyncpg://env:pass@db:5432/envdb"
    assert configured.telegram_bot_token.get_secret_value().startswith("987654321:")
    assert configured.content_encryption_key.get_secret_value() == "environment-secret"
    assert configured.raw_content_retention_days == 45


def test_secret_settings_are_redacted(settings: Settings) -> None:
    configured = settings.model_copy(
        update={
            "telegram_bot_token": SecretStr("telegram-plaintext-secret"),
            "telegram_webhook_secret": SecretStr("webhook-plaintext-secret"),
            "openai_api_key": SecretStr("openai-plaintext-secret"),
            "content_encryption_key": SecretStr("encryption-plaintext-secret"),
        }
    )

    rendered = f"{configured!r}\n{configured}\n{configured.model_dump_json()}"

    assert "plaintext-secret" not in rendered
    assert "**********" in rendered
