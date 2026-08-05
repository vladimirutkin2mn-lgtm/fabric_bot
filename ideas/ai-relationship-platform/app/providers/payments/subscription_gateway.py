"""Provider-neutral recurring billing contracts.

Vendor SDK objects, raw webhook payloads and plaintext payment methods never cross this
boundary. Gateways return only authoritative commercial and lifecycle facts; a saved
payment method may cross only as an authenticated encrypted envelope.
"""

import calendar
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Protocol
from uuid import UUID


@dataclass(frozen=True)
class CreateSubscriptionCheckout:
    user_id: UUID
    order_id: UUID
    product_code: str
    product_version: int
    amount_minor: int
    currency: str
    credits: int
    price_reference: str
    market: str
    consent_version: str
    idempotency_key: str
    success_url: str
    cancel_url: str
    receipt_contact: str | None = None


@dataclass(frozen=True)
class HostedSubscriptionCheckout:
    checkout_id: str
    url: str
    status: str
    expires_at: datetime | None = None
    live_mode: bool | None = None


@dataclass(frozen=True)
class RenewSubscription:
    user_id: UUID
    subscription_id: UUID
    provider_subscription_id: str
    product_code: str
    product_version: int
    amount_minor: int
    currency: str
    credits: int
    price_reference: str
    market: str
    consent_version: str
    period_start: datetime
    period_end: datetime
    idempotency_key: str
    encrypted_payment_method: bytes | None = None
    receipt_contact: str | None = None


@dataclass(frozen=True)
class PaidSubscriptionFact:
    user_id: UUID
    initial_order_id: UUID | None
    provider: str
    provider_customer_id: str
    provider_subscription_id: str
    provider_invoice_id: str
    provider_payment_id: str
    product_code: str
    product_version: int
    market: str
    currency: str
    amount_minor: int
    credits: int
    price_reference: str
    period_start: datetime
    period_end: datetime
    paid_at: datetime
    consent_version: str
    live_mode: bool | None = None
    encrypted_payment_method: bytes | None = None


@dataclass(frozen=True)
class PastDueSubscriptionFact:
    provider: str
    provider_subscription_id: str
    provider_invoice_id: str
    product_code: str
    product_version: int
    currency: str
    amount_minor: int
    credits: int
    period_start: datetime
    period_end: datetime


@dataclass(frozen=True)
class SubscriptionStateFact:
    user_id: UUID
    provider: str
    provider_subscription_id: str
    status: str
    current_period_start: datetime | None
    current_period_end: datetime | None
    cancel_at_period_end: bool
    canceled_at: datetime | None = None


@dataclass(frozen=True)
class InitialSubscriptionFailedFact:
    user_id: UUID
    order_id: UUID
    provider: str
    provider_payment_id: str
    provider_status: str


type SubscriptionProviderFact = (
    PaidSubscriptionFact
    | PastDueSubscriptionFact
    | SubscriptionStateFact
    | InitialSubscriptionFailedFact
)


def next_month_boundary(value: datetime) -> datetime:
    """Advance one calendar month while preserving UTC time and clamping the day."""
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("subscription boundary must be timezone-aware")
    current = value.astimezone(UTC)
    year = current.year + (1 if current.month == 12 else 0)
    month = 1 if current.month == 12 else current.month + 1
    day = min(current.day, calendar.monthrange(year, month)[1])
    return current.replace(year=year, month=month, day=day)


class SubscriptionGateway(Protocol):
    async def create_subscription_checkout(
        self, request: CreateSubscriptionCheckout
    ) -> HostedSubscriptionCheckout: ...

    async def fetch_subscription_event(
        self, event_type: str, object_id: str
    ) -> SubscriptionProviderFact: ...

    async def fetch_subscription(self, subscription_id: str) -> SubscriptionProviderFact: ...

    async def cancel_subscription(self, subscription_id: str) -> SubscriptionStateFact: ...

    async def resume_subscription(self, subscription_id: str) -> SubscriptionStateFact: ...


class MerchantManagedSubscriptionGateway(SubscriptionGateway, Protocol):
    async def renew_subscription(self, request: RenewSubscription) -> SubscriptionProviderFact: ...
