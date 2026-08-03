"""Framework-independent payment boundary."""

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Protocol
from uuid import UUID


@dataclass(frozen=True)
class CheckoutRequest:
    order_id: UUID
    checkout_token: UUID
    product_code: str
    amount_minor: int
    currency: str


@dataclass(frozen=True)
class Checkout:
    provider: str
    provider_checkout_id: str
    url: str


@dataclass(frozen=True)
class PaymentEvent:
    provider: str
    event_id: str
    checkout_id: str
    payment_id: str
    status: str
    amount_minor: int
    currency: str


class PaymentProviderError(Exception):
    pass


class PaymentSignatureError(PaymentProviderError):
    pass


class PaymentPayloadError(PaymentProviderError):
    pass


class PaymentExpiredEventError(PaymentProviderError):
    pass


class PaymentProvider(Protocol):
    async def create_checkout(self, request: CheckoutRequest) -> Checkout: ...
    async def verify_webhook(self, payload: bytes, headers: Mapping[str, str]) -> PaymentEvent: ...
