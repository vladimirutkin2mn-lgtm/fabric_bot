"""Server-owned product catalog; callbacks and webhooks never define value."""

from dataclasses import dataclass
from enum import StrEnum

from app.config import Settings


class ProductCode(StrEnum):
    ANALYSIS_SINGLE = "analysis_single"
    ANALYSIS_PACK_5 = "analysis_pack_5"
    SUBSCRIPTION_MONTHLY = "subscription_monthly"


@dataclass(frozen=True)
class Product:
    code: ProductCode
    title: str
    credits: int
    amount_minor: int
    currency: str
    recurring: bool = False


class ProductCatalog:
    def __init__(self, settings: Settings) -> None:
        cost = settings.analysis_price_credits
        subscription_title = (
            "Месячная подписка с автопродлением"
            if settings.subscriptions_enabled
            else "Месячный запас кредитов (без автопродления)"
        )
        self._products = {
            ProductCode.ANALYSIS_SINGLE: Product(
                ProductCode.ANALYSIS_SINGLE,
                "Один полный разбор",
                cost,
                settings.product_analysis_single_price_minor,
                settings.payment_currency,
            ),
            ProductCode.ANALYSIS_PACK_5: Product(
                ProductCode.ANALYSIS_PACK_5,
                "Пять полных разборов",
                cost * 5,
                settings.product_analysis_pack_5_price_minor,
                settings.payment_currency,
            ),
            ProductCode.SUBSCRIPTION_MONTHLY: Product(
                ProductCode.SUBSCRIPTION_MONTHLY,
                subscription_title,
                settings.product_subscription_monthly_credits,
                settings.product_subscription_monthly_price_minor,
                settings.payment_currency,
                recurring=settings.subscriptions_enabled,
            ),
        }

    def get(self, code: str | ProductCode) -> Product | None:
        try:
            return self._products.get(ProductCode(code))
        except ValueError:
            return None

    def all(self) -> tuple[Product, ...]:
        return tuple(self._products.values())


def format_minor(amount: int, currency: str) -> str:
    """Format integer minor units without binary floating point."""
    return f"{amount // 100},{amount % 100:02d} {currency}"
