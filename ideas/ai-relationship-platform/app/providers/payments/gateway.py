"""Small provider-neutral boundary for production one-time checkout."""

from dataclasses import dataclass
from datetime import datetime
from typing import Protocol


@dataclass(frozen=True)
class CreateCheckout:
    order_id: str
    product_code: str
    product_version: int
    amount_minor: int
    currency: str
    price_reference: str
    idempotency_key: str
    success_url: str
    cancel_url: str
    receipt_contact: str | None = None


@dataclass(frozen=True)
class HostedCheckout:
    checkout_id: str
    url: str
    status: str
    payment_id: str | None = None
    request_id: str | None = None
    expires_at: datetime | None = None
    live_mode: bool | None = None


@dataclass(frozen=True)
class AuthoritativePayment:
    checkout_id: str
    payment_id: str
    status: str
    amount_minor: int
    currency: str
    order_id: str
    mode: str = "payment"
    paid: bool = False
    live_mode: bool | None = None
    provider_status: str | None = None


class OneTimePaymentGateway(Protocol):
    async def create_checkout(self, request: CreateCheckout) -> HostedCheckout: ...
    async def fetch_payment(self, checkout_id: str) -> AuthoritativePayment: ...
