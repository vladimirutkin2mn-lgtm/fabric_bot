from app.domain.products import ProductCatalog, ProductCode, format_minor


def test_catalog_uses_server_settings(settings) -> None:
    catalog = ProductCatalog(settings)
    assert tuple(product.code for product in catalog.all()) == tuple(ProductCode)
    assert catalog.get("analysis_single").credits == settings.analysis_price_credits
    assert catalog.get("analysis_pack_5").credits == settings.analysis_price_credits * 5
    monthly = catalog.get("subscription_monthly")
    assert monthly is not None and monthly.credits == 30 and monthly.recurring is False
    assert catalog.get("unknown") is None


def test_minor_unit_formatting_never_uses_float() -> None:
    assert format_minor(19_900, "RUB") == "199,00 RUB"
