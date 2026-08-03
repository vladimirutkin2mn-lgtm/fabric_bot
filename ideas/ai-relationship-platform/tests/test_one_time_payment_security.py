"""Provider-independent security regression tests for one-time payments."""

import pytest

from app.api.webhooks import resolve_source_ip, source_is_allowed
from app.providers.payments.base import PaymentPayloadError, PaymentSignatureError
from app.services.checkout_service import ReceiptContactCipher


def test_yookassa_direct_and_trusted_proxy_resolution() -> None:
    assert resolve_source_ip("185.71.76.1", {}, "") == "185.71.76.1"
    assert (
        resolve_source_ip("10.0.0.2", {"x-forwarded-for": "185.71.76.1, 10.0.0.1"}, "10.0.0.0/8")
        == "185.71.76.1"
    )
    assert source_is_allowed("185.71.76.1", "185.71.76.0/27")
    assert not source_is_allowed("203.0.113.1", "185.71.76.0/27")


def test_yookassa_spoofed_and_malformed_forwarding_is_rejected() -> None:
    with pytest.raises(PaymentSignatureError):
        resolve_source_ip("203.0.113.4", {"x-forwarded-for": "185.71.76.1"}, "10.0.0.0/8")
    with pytest.raises(PaymentPayloadError):
        resolve_source_ip("10.0.0.2", {"x-forwarded-for": "garbage"}, "10.0.0.0/8")
    with pytest.raises(PaymentPayloadError):
        resolve_source_ip(
            "10.0.0.2",
            {"forwarded": "for=185.71.76.1", "x-forwarded-for": "185.71.76.1"},
            "10.0.0.0/8",
        )


def test_receipt_contact_is_encrypted_and_repr_does_not_leak() -> None:
    contact = "buyer@example.test"
    cipher = ReceiptContactCipher("test-key")
    encrypted = cipher.encrypt(contact)
    assert contact.encode() not in encrypted
    assert contact not in repr(encrypted)
    assert cipher.decrypt(encrypted) == contact
