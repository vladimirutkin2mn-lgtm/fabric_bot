"""Versioned, server-authoritative billing offers and routing."""

from dataclasses import dataclass
from enum import StrEnum

from app.config import Settings
from app.domain.products import ProductCode
from app.providers.payments.base import BillingMarket, PaymentProviderName


class PurchaseMode(StrEnum):
    ONE_TIME = "one_time"
    SUBSCRIPTION = "subscription"


@dataclass(frozen=True)
class BillingOffer:
    product_code: ProductCode
    product_version: int
    purchase_mode: PurchaseMode
    credits: int
    market: BillingMarket
    provider: PaymentProviderName
    currency: str
    amount_minor: int
    price_reference: str
    billing_interval: str | None = None


class BillingCatalog:
    """Catalog values, rather than request data, define commercial terms."""

    def __init__(self, settings: Settings) -> None:
        cost = settings.analysis_price_credits
        products = (
            (ProductCode.ANALYSIS_SINGLE, cost, settings.product_analysis_single_price_minor),
            (ProductCode.ANALYSIS_PACK_5, cost * 5, settings.product_analysis_pack_5_price_minor),
            (
                ProductCode.SUBSCRIPTION_MONTHLY,
                settings.product_subscription_monthly_credits,
                settings.product_subscription_monthly_price_minor,
            ),
        )
        self._offers: dict[tuple[ProductCode, BillingMarket, str], BillingOffer] = {}
        for code, credits, rub_amount in products:
            subscription = code is ProductCode.SUBSCRIPTION_MONTHLY
            self._add(
                code,
                credits,
                BillingMarket.RU,
                PaymentProviderName.YOOKASSA,
                "RUB",
                rub_amount,
                f"catalog:{code}:rub:v1",
                subscription,
            )
            for currency in ("EUR", "USD"):
                attr = f"stripe_price_{code.value}_{currency.lower()}"
                reference = getattr(settings, attr)
                # Price IDs are authoritative provider references. Empty references keep an
                # offer visible to validation/routing while live flags remain disabled.
                self._add(
                    code,
                    credits,
                    BillingMarket.INTERNATIONAL,
                    PaymentProviderName.STRIPE,
                    currency,
                    rub_amount,
                    reference or f"unconfigured:{code}:{currency}",
                    subscription,
                )

    def _add(
        self,
        code: ProductCode,
        credits: int,
        market: BillingMarket,
        provider: PaymentProviderName,
        currency: str,
        amount: int,
        reference: str,
        subscription: bool,
    ) -> None:
        self._offers[(code, market, currency)] = BillingOffer(
            code,
            1,
            PurchaseMode.SUBSCRIPTION if subscription else PurchaseMode.ONE_TIME,
            credits,
            market,
            provider,
            currency,
            amount,
            reference,
            "month" if subscription else None,
        )

    def resolve_product_offer(
        self, product_code: str | ProductCode, market: BillingMarket | str, currency: str
    ) -> BillingOffer:
        """Resolve only the exact server-owned market/currency route."""
        try:
            key = (ProductCode(product_code), BillingMarket(market), currency)
        except ValueError as exc:
            raise LookupError("unknown billing offer") from exc
        try:
            return self._offers[key]
        except KeyError as exc:
            raise LookupError("unsupported market/currency combination") from exc
