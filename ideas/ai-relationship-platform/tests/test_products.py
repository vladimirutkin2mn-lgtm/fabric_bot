from app.config import Settings
from app.domain.products import ProductCatalog, ProductCode, format_minor


def test_catalog_uses_server_settings(settings: Settings) -> None:
    catalog = ProductCatalog(settings)
    assert tuple(product.code for product in catalog.all()) == tuple(ProductCode)
    single = catalog.get("analysis_single")
    pack = catalog.get("analysis_pack_5")
    assert single is not None and single.credits == settings.analysis_price_credits
    assert pack is not None and pack.credits == settings.analysis_price_credits * 5
    monthly = catalog.get("subscription_monthly")
    assert monthly is not None and monthly.credits == 30 and monthly.recurring is False
    assert catalog.get("unknown") is None


def test_minor_unit_formatting_never_uses_float() -> None:
    assert format_minor(19_900, "RUB") == "199,00 RUB"
