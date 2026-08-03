import json
import time
from uuid import uuid4

import pytest

from app.providers.payments.base import (
    CheckoutRequest,
    PaymentExpiredEventError,
    PaymentPayloadError,
    PaymentSignatureError,
)
from app.providers.payments.mock import MockPaymentProvider


def _payload(amount: object = 19900, currency: object = "RUB") -> bytes:
    return json.dumps(
        {
            "event_id": "event",
            "checkout_id": "checkout",
            "payment_id": "payment",
            "status": "paid",
            "amount_minor": amount,
            "currency": currency,
        }
    ).encode()


async def test_checkout_url_contains_only_opaque_token() -> None:
    provider = MockPaymentProvider("http://localhost:8000", "secret")
    token, order = uuid4(), uuid4()
    checkout = await provider.create_checkout(
        CheckoutRequest(order, token, "analysis_single", 19900, "RUB")
    )
    assert checkout.url == f"http://localhost:8000/payments/mock/checkout/{token}"
    assert str(order) not in checkout.url and "analysis_single" not in checkout.url


async def test_valid_hmac_and_strict_values() -> None:
    provider = MockPaymentProvider("http://localhost:8000", "secret")
    payload, timestamp = _payload(), int(time.time())
    event = await provider.verify_webhook(
        payload,
        {"X-Mock-Timestamp": str(timestamp), "X-Mock-Signature": provider.sign(payload, timestamp)},
    )
    assert event.amount_minor == 19900
    for invalid in ("19900", 19900.0, True):
        body = _payload(invalid)
        with pytest.raises(PaymentPayloadError):
            await provider.verify_webhook(
                body,
                {
                    "X-Mock-Timestamp": str(timestamp),
                    "X-Mock-Signature": provider.sign(body, timestamp),
                },
            )


async def test_signature_and_replay_window() -> None:
    provider = MockPaymentProvider("http://localhost:8000", "secret", 10)
    payload, timestamp = _payload(), int(time.time())
    with pytest.raises(PaymentSignatureError):
        await provider.verify_webhook(
            payload, {"X-Mock-Timestamp": str(timestamp), "X-Mock-Signature": "bad"}
        )
    old = timestamp - 11
    with pytest.raises(PaymentExpiredEventError):
        await provider.verify_webhook(
            payload, {"X-Mock-Timestamp": str(old), "X-Mock-Signature": provider.sign(payload, old)}
        )
    future = timestamp + 11
    with pytest.raises(PaymentExpiredEventError):
        await provider.verify_webhook(
            payload,
            {"X-Mock-Timestamp": str(future), "X-Mock-Signature": provider.sign(payload, future)},
        )
