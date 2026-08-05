import pytest
from pydantic import SecretStr, ValidationError

from app.config import Settings
from app.domain.billing import BillingCatalog


def base(**changes: object) -> Settings:
    values: dict[str, object] = {
        "app_env": "test",
        "database_url": "postgresql+asyncpg://u:p@db/x",
        "telegram_bot_token": SecretStr("123456789:AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA"),
        "content_encryption_key": SecretStr("test-only-strong-content-key-32-bytes"),
        "stripe_price_subscription_monthly_eur": "price_monthly_eur",
        "stripe_amount_subscription_monthly_eur_minor": 990,
    }
    values.update(changes)
    return Settings(**values)  # type: ignore[arg-type]


@pytest.mark.parametrize(
    ("price", "amount"),
    [("price_monthly_eur", None), ("", 990)],
)
def test_subscription_price_and_expected_amount_are_atomic(
    price: str, amount: int | None
) -> None:
    with pytest.raises(ValidationError):
        base(
            stripe_price_subscription_monthly_eur=price,
            stripe_amount_subscription_monthly_eur_minor=amount,
        )


def test_catalog_uses_exact_configured_subscription_amount() -> None:
    settings = base()
    offer = BillingCatalog(settings).resolve_product_offer(
        "subscription_monthly", "INTERNATIONAL", "EUR"
    )
    assert offer.amount_minor == 990
    assert offer.price_reference == "price_monthly_eur"
    assert offer.billing_interval == "month"


def test_unconfigured_subscription_currency_remains_unavailable_to_checkout() -> None:
    settings = base()
    offer = BillingCatalog(settings).resolve_product_offer(
        "subscription_monthly", "INTERNATIONAL", "USD"
    )
    assert offer.price_reference.startswith("unconfigured:")
    assert offer.amount_minor == 1
