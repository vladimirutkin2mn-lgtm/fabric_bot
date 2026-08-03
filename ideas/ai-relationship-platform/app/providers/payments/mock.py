"""Offline mock checkout with timestamped HMAC-SHA256 events."""

import hashlib
import hmac
import json
import time
from collections.abc import Mapping
from typing import Any

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
            value: Any = json.loads(payload)
            event = PaymentEvent(
                provider="mock",
                event_id=str(value["event_id"]),
                checkout_id=str(value["checkout_id"]),
                payment_id=str(value["payment_id"]),
                status=str(value["status"]),
                amount_minor=int(value["amount_minor"]),
                currency=str(value["currency"]),
            )
        except (ValueError, TypeError, KeyError, json.JSONDecodeError) as exc:
            raise PaymentPayloadError from exc
        if (
            event.status not in {"paid", "failed"}
            or event.amount_minor <= 0
            or len(event.currency) != 3
        ):
            raise PaymentPayloadError
        return event
