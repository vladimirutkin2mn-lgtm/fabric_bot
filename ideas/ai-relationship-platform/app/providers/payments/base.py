"""Provider-neutral payment types. No vendor SDK types cross this boundary."""

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from typing import Protocol
from uuid import UUID


class PaymentProviderName(StrEnum):
    MOCK = "mock"
    YOOKASSA = "yookassa"
    STRIPE = "stripe"


class BillingMarket(StrEnum):
    RU = "RU"
    INTERNATIONAL = "INTERNATIONAL"


class PaymentMode(StrEnum):
    ONE_TIME = "one_time"
    SUBSCRIPTION_INITIAL = "subscription_initial"
    SUBSCRIPTION_RENEWAL = "subscription_renewal"


class PaymentStatus(StrEnum):
    PENDING = "pending"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    CANCELED = "canceled"


class RefundStatus(StrEnum):
    PENDING = "pending"
    SUCCEEDED = "succeeded"
    FAILED = "failed"


@dataclass(frozen=True)
class CheckoutRequest:
    order_id: UUID
    checkout_token: UUID
    product_code: str
    amount_minor: int
    currency: str
    mode: PaymentMode = PaymentMode.ONE_TIME


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


@dataclass(frozen=True)
class PaymentResult:
    provider_payment_id: str
    status: PaymentStatus
    amount_minor: int
    currency: str


@dataclass(frozen=True)
class SubscriptionResult:
    provider_subscription_id: str
    status: str
    current_period_end: datetime | None = None


@dataclass(frozen=True)
class RefundResult:
    provider_refund_id: str
    status: RefundStatus
    amount_minor: int
    currency: str


@dataclass(frozen=True)
class VerifiedWebhookResult:
    provider: PaymentProviderName
    event_id: str
    event_type: str
    object_id: str
    payload_hash: str


class PaymentProviderError(Exception):
    pass


class PaymentSignatureError(PaymentProviderError):
    pass


class PaymentPayloadError(PaymentProviderError):
    pass


class PaymentExpiredEventError(PaymentProviderError):
    pass


class PaymentProvider(Protocol):
    """Compatibility boundary used by the existing offline checkout flow."""

    async def create_checkout(self, request: CheckoutRequest) -> Checkout: ...
    async def verify_webhook(self, payload: bytes, headers: Mapping[str, str]) -> PaymentEvent: ...


class ProductionPaymentProvider(PaymentProvider, Protocol):
    """Full typed contract for production adapters introduced in later milestones."""

    async def create_subscription_checkout(self, request: CheckoutRequest) -> Checkout: ...
    async def fetch_payment(self, payment_id: str) -> PaymentResult: ...
    async def fetch_subscription(self, subscription_id: str) -> SubscriptionResult: ...
    async def fetch_refund(self, refund_id: str) -> RefundResult: ...
    async def cancel_subscription(self, subscription_id: str) -> SubscriptionResult: ...
    async def create_recurring_payment(self, request: CheckoutRequest) -> PaymentResult: ...
    async def create_refund(
        self, payment_id: str, amount_minor: int, currency: str, idempotency_key: str
    ) -> RefundResult: ...
    async def verify_webhook(self, payload: bytes, headers: Mapping[str, str]) -> PaymentEvent: ...
