"""Offline mock checkout with timestamped HMAC-SHA256 events."""

import hashlib
import hmac
import json
import time
from collections.abc import Mapping

from app.providers.payments.base import (
    Checkout,
    CheckoutRequest,
    PaymentEvent,
    PaymentExpiredEventError,
    PaymentPayloadError,
    PaymentSignatureError,
)


class MockPaymentProvider:
    name = "mock"

    def __init__(self, public_base_url: str, secret: str, max_age_seconds: int = 300) -> None:
        self._base, self._secret, self._max_age = (
            public_base_url.rstrip("/"),
            secret.encode(),
            max_age_seconds,
        )

    async def create_checkout(self, request: CheckoutRequest) -> Checkout:
        # The durable order ID is the provider idempotency key, so retries return
        # the same checkout identity and opaque local URL.
        checkout_id = f"mock-{request.order_id}"
        return Checkout(
            self.name, checkout_id, f"{self._base}/payments/mock/checkout/{request.checkout_token}"
        )

    def sign(self, payload: bytes, timestamp: int) -> str:
        return hmac.new(
            self._secret, str(timestamp).encode() + b"." + payload, hashlib.sha256
        ).hexdigest()

    async def verify_webhook(self, payload: bytes, headers: Mapping[str, str]) -> PaymentEvent:
        try:
            timestamp = int(headers["X-Mock-Timestamp"])
        except (KeyError, ValueError) as exc:
            raise PaymentSignatureError from exc
        signature = headers.get("X-Mock-Signature", "")
        if not hmac.compare_digest(signature, self.sign(payload, timestamp)):
            raise PaymentSignatureError
        if abs(int(time.time()) - timestamp) > self._max_age:
            raise PaymentExpiredEventError
        try:
            value = json.loads(payload)
            if not isinstance(value, dict):
                raise PaymentPayloadError
            required = (
                "event_id",
                "checkout_id",
                "payment_id",
                "status",
                "amount_minor",
                "currency",
            )
            if any(key not in value for key in required):
                raise PaymentPayloadError
            identifiers = (value["event_id"], value["checkout_id"], value["payment_id"])
            if any(
                not isinstance(item, str) or not item or len(item) > 255 for item in identifiers
            ):
                raise PaymentPayloadError
            amount = value["amount_minor"]
            currency = value["currency"]
            status = value["status"]
            if isinstance(amount, bool) or not isinstance(amount, int) or amount <= 0:
                raise PaymentPayloadError
            if (
                not isinstance(currency, str)
                or len(currency) != 3
                or not currency.isascii()
                or not currency.isalpha()
                or not currency.isupper()
            ):
                raise PaymentPayloadError
            if status not in {"paid", "failed"}:
                raise PaymentPayloadError
            event = PaymentEvent(
                provider="mock",
                event_id=value["event_id"],
                checkout_id=value["checkout_id"],
                payment_id=value["payment_id"],
                status=status,
                amount_minor=amount,
                currency=currency,
            )
        except (ValueError, TypeError, KeyError, json.JSONDecodeError) as exc:
            raise PaymentPayloadError from exc
        return event
