"""Receipt FSM data-safety regressions for production Telegram checkout."""

import pytest

from app.bot.keyboards import payment_market_keyboard, receipt_contact_keyboard
from app.services.receipt_contact import InvalidReceiptContact, validate_receipt_contact


def test_receipt_callback_data_never_contains_contact() -> None:
    contact = "buyer@example.com"
    keyboards = [payment_market_keyboard("analysis_single"), receipt_contact_keyboard()]
    callbacks = [
        button.callback_data
        for keyboard in keyboards
        for row in keyboard.inline_keyboard
        for button in row
        if button.callback_data
    ]
    assert callbacks
    assert all(contact not in value for value in callbacks)


@pytest.mark.parametrize("contact", ["buyer@example.com", "+79991234567"])
def test_receipt_contact_validation_for_handler(contact: str) -> None:
    assert validate_receipt_contact(contact).value == contact


def test_invalid_receipt_contact_is_retryable() -> None:
    with pytest.raises(InvalidReceiptContact):
        validate_receipt_contact("invalid")
