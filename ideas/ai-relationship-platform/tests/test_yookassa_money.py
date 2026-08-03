import pytest

from app.providers.payments.yookassa_gateway import parse_minor_amount


@pytest.mark.parametrize(
    ("value", "minor"),
    [
        ("199.00", 19_900),
        ("0.01", 1),
        ("999999999999.99", 99_999_999_999_999),
    ],
)
def test_decimal_minor_amount(value: str, minor: int) -> None:
    assert parse_minor_amount(value) == minor


@pytest.mark.parametrize("value", [None, "", "wat", "-0.01", "1.001", "NaN", "Infinity"])
def test_invalid_decimal_amount(value: object) -> None:
    with pytest.raises(ValueError):
        parse_minor_amount(value)
