"""Configuration tests."""

import pytest
from pydantic import SecretStr, ValidationError
from pytest import MonkeyPatch

from app.config import Settings
from app.providers.llm.factory import create_llm_client
from app.providers.llm.openai import OpenAILLMClient
from app.providers.llm.stub import StubLLMClient


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

    configured = Settings(_env_file=None)

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


def test_stub_provider_needs_no_api_key(settings: Settings) -> None:
    assert isinstance(create_llm_client(settings), StubLLMClient)


def test_openai_provider_requires_key_without_exposing_secrets(settings: Settings) -> None:
    missing = settings.model_copy(
        update={"llm_provider": "openai", "openai_api_key": SecretStr("")}
    )
    with pytest.raises(ValueError) as caught:
        create_llm_client(missing)
    assert "API_KEY" in str(caught.value) and "plaintext" not in str(caught.value)
    configured = missing.model_copy(update={"openai_api_key": SecretStr("plaintext-secret")})
    assert isinstance(create_llm_client(configured), OpenAILLMClient)


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("llm_provider", "xai"),
        ("llm_timeout_seconds", 0),
        ("llm_max_transport_attempts", 0),
        ("llm_max_transport_attempts", 6),
        ("llm_max_repair_attempts", -1),
        ("llm_max_repair_attempts", 2),
    ],
)
def test_llm_settings_reject_unsupported_or_unbounded_values(
    settings: Settings, field: str, value: object
) -> None:
    values = settings.model_dump()
    values[field] = value
    with pytest.raises(ValidationError):
        Settings.model_validate(values)


def test_model_prompt_and_policy_are_configurable(settings: Settings) -> None:
    configured = settings.model_copy(
        update={
            "llm_model": "configured-model",
            "llm_prompt_version": "analysis_v2",
            "llm_max_repair_attempts": 0,
        }
    )
    assert (
        configured.llm_model,
        configured.llm_prompt_version,
        configured.llm_max_repair_attempts,
    ) == ("configured-model", "analysis_v2", 0)
