"""Configuration tests."""

from app.config import Settings


def test_empty_webhook_uses_polling(settings: Settings) -> None:
    assert settings.webhook_enabled is False


def test_webhook_url_enables_webhook(settings: Settings) -> None:
    configured = settings.model_copy(update={"telegram_webhook_url": "https://example.com/hook"})
    assert configured.webhook_enabled is True
