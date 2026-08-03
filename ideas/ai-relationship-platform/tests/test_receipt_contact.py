import pytest

from app.services.receipt_contact import InvalidReceiptContact, validate_receipt_contact


@pytest.mark.parametrize(
    "value", ["buyer@example.com", "first.last+tag@example.co.uk", "+79991234567"]
)
def test_valid_receipt_contact_is_typed_and_redacted(value: str) -> None:
    contact = validate_receipt_contact(value)
    assert contact.value == value
    assert value not in repr(contact)


@pytest.mark.parametrize(
    "value", ["", "buyer@", "@example.com", "79991234567", "+012345678", "+12 345"]
)
def test_invalid_receipt_contact(value: str) -> None:
    with pytest.raises(InvalidReceiptContact):
        validate_receipt_contact(value)
