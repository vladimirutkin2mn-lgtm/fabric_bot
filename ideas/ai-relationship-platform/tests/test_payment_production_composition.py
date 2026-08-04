from unittest.mock import patch

import pytest
from pydantic import SecretStr, ValidationError

from app.api.main import create_app
from app.config import Settings
from app.providers.payments.composition import create_payment_components


def production(**changes: object) -> Settings:
    values: dict[str, object] = {
        "app_env": "production",
        "database_url": "postgresql+asyncpg://u:p@db/x",
        "telegram_bot_token": SecretStr("123456789:AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA"),
        "content_encryption_key": SecretStr("content-key"),
        "billing_enabled": True,
        "payment_public_base_url": "https://pay.example",
        "payment_provider": "production",
    }
    values.update(changes)
    return Settings(**values)  # type: ignore[arg-type]


def stripe_values() -> dict[str, object]:
    return {
        "stripe_enabled": True,
        "stripe_secret_key": "sk_live_redacted",
        "stripe_webhook_secret": "whsec_redacted",
        "stripe_price_analysis_single_eur": "price_eur_1",
        "stripe_price_analysis_single_usd": "price_usd_1",
        "stripe_price_analysis_pack_5_eur": "price_eur_5",
        "stripe_price_analysis_pack_5_usd": "price_usd_5",
        "stripe_amount_analysis_single_eur_minor": 411,
        "stripe_amount_analysis_single_usd_minor": 577,
        "stripe_amount_analysis_pack_5_eur_minor": 1800,
        "stripe_amount_analysis_pack_5_usd_minor": 2200,
    }


@pytest.mark.parametrize("kind", ["yookassa", "stripe", "both"])
def test_production_composition_starts_without_mock(kind: str) -> None:
    values: dict[str, object] = {}
    if kind in {"yookassa", "both"}:
        values.update(
            yookassa_enabled=True,
            yookassa_shop_id="shop",
            yookassa_secret_key="secret",
            yookassa_webhook_ip_allowlist="185.71.76.0/27",
        )
    if kind in {"stripe", "both"}:
        values.update(stripe_values())
    settings = production(**values)
    with patch("app.providers.payments.composition.StripeGateway", autospec=True):
        components = create_payment_components(settings)
        app = create_app(settings)
    assert components.legacy is None
    assert app.state.payment_provider is None
    assert len(components.gateways) == (2 if kind == "both" else 1)


def test_mock_remains_forbidden_in_production() -> None:
    with pytest.raises(ValidationError):
        production(payment_provider="mock")


def test_enabled_stripe_requires_all_one_time_prices() -> None:
    values = stripe_values()
    values["stripe_price_analysis_pack_5_usd"] = ""
    with pytest.raises(ValidationError):
        production(**values)


def test_enabled_stripe_requires_explicit_expected_amounts() -> None:
    values = stripe_values()
    values["stripe_amount_analysis_pack_5_usd_minor"] = None
    with pytest.raises(ValidationError):
        production(**values)


@pytest.mark.parametrize("allowlist", ["", "not-a-network", "10.0.0.0/999"])
def test_yookassa_requires_valid_webhook_networks(allowlist: str) -> None:
    with pytest.raises(ValidationError):
        production(
            yookassa_enabled=True,
            yookassa_shop_id="shop",
            yookassa_secret_key="secret",
            yookassa_webhook_ip_allowlist=allowlist,
        )


def test_yookassa_accepts_ipv4_ipv6_and_trusted_proxy_networks() -> None:
    settings = production(
        yookassa_enabled=True,
        yookassa_shop_id="shop",
        yookassa_secret_key="secret",
        yookassa_webhook_ip_allowlist="185.71.76.0/27,2a02:5180::/32",
        yookassa_trusted_proxy_allowlist="10.0.0.0/8,2001:db8::/32",
    )
    assert settings.yookassa_enabled


def test_production_billing_requires_at_least_one_gateway() -> None:
    with pytest.raises(ValidationError):
        production()
