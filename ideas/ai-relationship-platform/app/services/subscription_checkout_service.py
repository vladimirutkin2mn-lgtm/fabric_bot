"""Crash-safe, server-authoritative subscription checkout orchestration."""

from dataclasses import dataclass
from datetime import UTC, datetime
from uuid import UUID, uuid4

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.config import Settings
from app.db.models import BillingJob, BillingOutboxEvent, PaymentOrder, User
from app.domain.billing import BillingCatalog, PurchaseMode
from app.providers.payments.base import (
    BillingMarket,
    PaymentProviderName,
    PermanentProviderError,
    UnknownProviderOutcome,
)
from app.providers.payments.subscription_gateway import (
    CreateSubscriptionCheckout,
    HostedSubscriptionCheckout,
    SubscriptionGateway,
)


class SubscriptionCheckoutRejected(RuntimeError):
    """Safe user-facing rejection without provider detail."""


@dataclass(frozen=True)
class SubscriptionCheckoutResult:
    order_id: UUID
    checkout_token: UUID
    url: str | None
    status: str


class SubscriptionCheckoutService:
    def __init__(
        self,
        sessions: async_sessionmaker[AsyncSession],
        settings: Settings,
        catalog: BillingCatalog,
        gateways: dict[PaymentProviderName, SubscriptionGateway],
    ) -> None:
        self._sessions = sessions
        self._settings = settings
        self._catalog = catalog
        self._gateways = gateways

    async def create_checkout(
        self,
        user_id: UUID,
        product_code: str,
        market: BillingMarket | str,
        currency: str,
    ) -> SubscriptionCheckoutResult:
        if not self._settings.permits_new_checkout() or not self._settings.subscriptions_enabled:
            raise SubscriptionCheckoutRejected("subscriptions unavailable")
        try:
            offer = self._catalog.resolve_product_offer(product_code, market, currency)
        except LookupError as exc:
            raise SubscriptionCheckoutRejected("unsupported offer") from exc
        if offer.purchase_mode is not PurchaseMode.SUBSCRIPTION:
            raise SubscriptionCheckoutRejected("not a subscription offer")
        if offer.provider not in self._gateways:
            raise SubscriptionCheckoutRejected("subscription provider unavailable")
        if offer.price_reference.startswith("unconfigured:"):
            raise SubscriptionCheckoutRejected("subscription price unavailable")

        attempt = uuid4()
        now = datetime.now(UTC)
        async with self._sessions.begin() as session:
            user = await session.scalar(select(User).where(User.id == user_id).with_for_update())
            if user is None or user.privacy_status != "active":
                raise SubscriptionCheckoutRejected("active user not found")
            order = await session.scalar(
                select(PaymentOrder)
                .where(
                    PaymentOrder.user_id == user_id,
                    PaymentOrder.provider == offer.provider.value,
                    PaymentOrder.product_code == offer.product_code.value,
                    PaymentOrder.market == offer.market.value,
                    PaymentOrder.currency == offer.currency,
                    PaymentOrder.mode == "subscription_initial",
                    PaymentOrder.status.in_(("creating", "pending")),
                )
                .with_for_update()
            )
            if order and order.status == "pending" and order.checkout_url:
                return SubscriptionCheckoutResult(
                    order.id, order.checkout_token, order.checkout_url, order.status
                )
            if (
                order
                and order.checkout_creation_started_at
                and (now - order.checkout_creation_started_at).total_seconds()
                < self._settings.checkout_creation_lease_seconds
            ):
                return SubscriptionCheckoutResult(
                    order.id, order.checkout_token, order.checkout_url, order.status
                )
            if order is None:
                order = PaymentOrder(
                    user_id=user_id,
                    provider=offer.provider.value,
                    product_code=offer.product_code.value,
                    status="creating",
                    credits=offer.credits,
                    amount_minor=offer.amount_minor,
                    currency=offer.currency,
                    market=offer.market.value,
                    mode="subscription_initial",
                    product_version=offer.product_version,
                    billing_period="month",
                    commercial_snapshot={
                        "product_code": offer.product_code.value,
                        "product_version": offer.product_version,
                        "credits": offer.credits,
                        "amount_minor": offer.amount_minor,
                        "currency": offer.currency,
                        "provider": offer.provider.value,
                        "market": offer.market.value,
                        "price_reference": offer.price_reference,
                        "billing_period": "month",
                        "consent_version": self._settings.billing_consent_version,
                    },
                )
                session.add(order)
                await session.flush()
                order.idempotency_key = f"subscription:checkout:{order.id}:v1"
            order.checkout_creation_attempt_id = attempt
            order.checkout_creation_started_at = now
            request = CreateSubscriptionCheckout(
                user_id=user_id,
                order_id=order.id,
                product_code=order.product_code,
                product_version=order.product_version,
                amount_minor=order.amount_minor,
                currency=order.currency,
                credits=order.credits,
                price_reference=offer.price_reference,
                market=order.market,
                consent_version=self._settings.billing_consent_version,
                idempotency_key=order.idempotency_key or "",
                success_url=(
                    f"{self._settings.payment_public_base_url}/payments/return/"
                    f"{order.checkout_token}"
                ),
                cancel_url=(
                    f"{self._settings.payment_public_base_url}/payments/return/"
                    f"{order.checkout_token}"
                ),
            )
            order_id = order.id
            token = order.checkout_token
            provider = offer.provider

        try:
            checkout = await self._gateways[provider].create_subscription_checkout(request)
        except UnknownProviderOutcome:
            await self._unknown(order_id, attempt, provider.value)
            return SubscriptionCheckoutResult(order_id, token, None, "creating")
        except PermanentProviderError:
            await self._failed(order_id, attempt)
            raise SubscriptionCheckoutRejected("provider rejected checkout") from None
        await self._save(order_id, attempt, checkout)
        return SubscriptionCheckoutResult(order_id, token, checkout.url, "pending")

    async def _save(
        self, order_id: UUID, attempt: UUID, value: HostedSubscriptionCheckout
    ) -> None:
        async with self._sessions.begin() as session:
            initial = await session.get(PaymentOrder, order_id)
            if initial is None:
                return
            user = await session.scalar(
                select(User).where(User.id == initial.user_id).with_for_update()
            )
            order = await session.get(PaymentOrder, order_id, with_for_update=True)
            if order is None or order.checkout_creation_attempt_id != attempt:
                return
            if user is None or user.privacy_status != "active":
                order.status = "cancelled"
                order.failure_code = "user_deleted"
                order.checkout_url = None
                order.checkout_creation_attempt_id = None
                order.checkout_creation_started_at = None
                return
            order.provider_checkout_id = value.checkout_id
            order.checkout_url = value.url
            order.status = "pending"
            order.provider_status = value.status
            order.checkout_expires_at = value.expires_at
            order.provider_live_mode = value.live_mode
            session.add(
                BillingOutboxEvent(
                    aggregate_type="payment_order",
                    aggregate_id=str(order.id),
                    event_type="subscription_checkout_started",
                    payload={"product_code": order.product_code, "provider": order.provider},
                    idempotency_key=f"subscription_checkout_started:{order.id}",
                )
            )

    async def _unknown(self, order_id: UUID, attempt: UUID, provider: str) -> None:
        async with self._sessions.begin() as session:
            order = await session.get(PaymentOrder, order_id, with_for_update=True)
            if order is None or order.checkout_creation_attempt_id != attempt:
                return
            order.provider_status = "unknown"
            key = f"subscription:checkout:reconcile:{order_id}"
            job = await session.scalar(
                select(BillingJob).where(BillingJob.idempotency_key == key).with_for_update()
            )
            if job is None:
                session.add(
                    BillingJob(
                        job_type="subscription_checkout_reconciliation",
                        provider=provider,
                        object_type="payment_order",
                        object_id=str(order_id),
                        idempotency_key=key,
                    )
                )
            elif job.status in {"completed", "failed", "manual_review"}:
                job.status = "pending"
                job.available_at = datetime.now(UTC)
                job.last_error_code = None

    async def _failed(self, order_id: UUID, attempt: UUID) -> None:
        async with self._sessions.begin() as session:
            order = await session.get(PaymentOrder, order_id, with_for_update=True)
            if order is None or order.checkout_creation_attempt_id != attempt:
                return
            order.status = "failed"
            order.failure_code = "provider_rejected_subscription_checkout"
            session.add(
                BillingOutboxEvent(
                    aggregate_type="payment_order",
                    aggregate_id=str(order.id),
                    event_type="payment_failed",
                    payload={"product_code": order.product_code, "provider": order.provider},
                    idempotency_key=f"subscription_checkout_failed:{order.id}",
                )
            )
