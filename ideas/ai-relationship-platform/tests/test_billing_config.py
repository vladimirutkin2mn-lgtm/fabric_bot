import pytest
from pydantic import SecretStr, ValidationError

from app.config import Settings


def production(**values: object) -> Settings:
    defaults: dict[str, object] = {
        "app_env": "production",
        "database_url": "postgresql+asyncpg://u:p@db/x",
        "telegram_bot_token": SecretStr("123456789:AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA"),
        "content_encryption_key": SecretStr("x"),
        "payment_public_base_url": "https://pay.example",
        "payment_provider": "stripe",
    }
    defaults.update(values)
    return Settings(**defaults)  # type: ignore[arg-type]


@pytest.mark.parametrize(
    "values",
    [
        {"billing_enabled": True, "payment_public_base_url": "http://pay.example"},
        {"billing_enabled": True, "payment_provider": "mock"},
        {"refunds_enabled": True},
        {"yookassa_recurring_enabled": True},
        {
            "stripe_enabled": True,
            "stripe_secret_key": "sk_test_redacted",
            "stripe_webhook_secret": "whsec_x",
        },
        {"subscriptions_enabled": True},
    ],
)
def test_production_rejection_matrix(values: dict[str, object]) -> None:
    with pytest.raises(ValidationError):
        production(**values)


def test_kill_switch_blocks_new_work_not_ingress() -> None:
    settings = production(billing_enabled=True, billing_kill_switch=True)
    assert not settings.permits_new_checkout()
    assert not settings.permits_renewal()
    assert not settings.permits_refund()
    assert settings.permits_webhook_receipt()
    assert settings.permits_reconciliation()
