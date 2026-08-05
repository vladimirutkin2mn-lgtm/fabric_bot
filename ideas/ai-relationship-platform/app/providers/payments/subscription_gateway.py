"""Provider-neutral recurring billing contracts.

Vendor SDK objects, raw webhook payloads, payment methods and receipt contacts never
cross this boundary. Gateways return only authoritative commercial and lifecycle facts.
"""

from dataclasses import dataclass
from datetime import datetime
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


@dataclass(frozen=True)
class HostedSubscriptionCheckout:
    checkout_id: str
    url: str
    status: str
    expires_at: datetime | None = None
    live_mode: bool | None = None


@dataclass(frozen=True)
class PaidSubscriptionFact:
    user_id: UUID
    initial_order_id: UUID
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


type SubscriptionProviderFact = (
    PaidSubscriptionFact | PastDueSubscriptionFact | SubscriptionStateFact
)


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
