import pytest

from app.config import Settings
from app.domain.billing import BillingCatalog
from app.providers.payments.base import BillingMarket, PaymentProviderName


def test_authoritative_routes(settings: Settings) -> None:
    catalog = BillingCatalog(settings)
    assert (
        catalog.resolve_product_offer("analysis_single", BillingMarket.RU, "RUB").provider
        is PaymentProviderName.YOOKASSA
    )
    for currency in ("EUR", "USD"):
        assert (
            catalog.resolve_product_offer(
                "analysis_pack_5", BillingMarket.INTERNATIONAL, currency
            ).provider
            is PaymentProviderName.STRIPE
        )
    with pytest.raises(LookupError):
        catalog.resolve_product_offer("analysis_single", BillingMarket.RU, "USD")


def test_stripe_uses_currency_specific_expected_amounts(settings: Settings) -> None:
    configured = settings.model_copy(
        update={
            "stripe_amount_analysis_single_eur_minor": 411,
            "stripe_amount_analysis_single_usd_minor": 577,
        }
    )
    catalog = BillingCatalog(configured)
    eur = catalog.resolve_product_offer("analysis_single", BillingMarket.INTERNATIONAL, "EUR")
    usd = catalog.resolve_product_offer("analysis_single", BillingMarket.INTERNATIONAL, "USD")
    rub = catalog.resolve_product_offer("analysis_single", BillingMarket.RU, "RUB")
    assert (eur.amount_minor, usd.amount_minor, rub.amount_minor) == (411, 577, 19_900)
