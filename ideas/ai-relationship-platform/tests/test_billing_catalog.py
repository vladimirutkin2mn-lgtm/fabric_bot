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
