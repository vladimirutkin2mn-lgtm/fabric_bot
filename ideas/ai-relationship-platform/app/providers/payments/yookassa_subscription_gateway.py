"""Merchant-managed YooKassa subscription orchestration for the billing worker."""

from datetime import UTC, datetime

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.config import Settings
from app.db.models import PaymentOrder, Subscription
from app.providers.payments.base import PermanentProviderError
from app.providers.payments.subscription_gateway import (
    CreateSubscriptionCheckout,
    HostedSubscriptionCheckout,
    RenewSubscription,
    SubscriptionProviderFact,
    SubscriptionStateFact,
    next_month_boundary,
)
from app.providers.payments.yookassa_gateway import YooKassaGateway


class YooKassaSubscriptionGateway:
    """Build deterministic renewals from durable state; delegate provider I/O."""

    def __init__(
        self,
        sessions: async_sessionmaker[AsyncSession],
        settings: Settings,
        gateway: YooKassaGateway,
    ) -> None:
        self._sessions = sessions
        self._settings = settings
        self._gateway = gateway

    async def create_subscription_checkout(
        self, request: CreateSubscriptionCheckout
    ) -> HostedSubscriptionCheckout:
        return await self._gateway.create_subscription_checkout(request)

    async def renew_subscription(self, request: RenewSubscription) -> SubscriptionProviderFact:
        return await self._gateway.renew_subscription(request)

    async def fetch_subscription_event(
        self, event_type: str, object_id: str
    ) -> SubscriptionProviderFact:
        return await self._gateway.fetch_subscription_event(event_type, object_id)

    async def fetch_subscription(self, subscription_id: str) -> SubscriptionProviderFact:
        async with self._sessions() as session:
            subscription = await session.scalar(
                select(Subscription).where(
                    Subscription.provider == "yookassa",
                    Subscription.provider_subscription_id == subscription_id,
                )
            )
            if subscription is None or subscription.current_period_end is None:
                raise PermanentProviderError("subscription_not_found")
            order = (
                await session.get(PaymentOrder, subscription.last_order_id)
                if subscription.last_order_id is not None
                else None
            )
            if order is None:
                raise PermanentProviderError("subscription_order_missing")
            current_period_end = subscription.current_period_end.astimezone(UTC)
            provider_payment_id = order.provider_payment_id
            snapshot = dict(order.commercial_snapshot)
            renewal = RenewSubscription(
                user_id=subscription.user_id,
                subscription_id=subscription.id,
                provider_subscription_id=subscription.provider_subscription_id,
                product_code=subscription.product_code,
                product_version=subscription.product_version,
                amount_minor=order.amount_minor,
                currency=order.currency,
                credits=order.credits,
                price_reference=str(snapshot.get("price_reference", "")),
                market=order.market,
                consent_version=subscription.consent_version,
                period_start=current_period_end,
                period_end=next_month_boundary(current_period_end),
                idempotency_key=(
                    f"subscription:renewal:{subscription.id}:"
                    f"{current_period_end.isoformat()}:payment:v1"
                ),
                encrypted_payment_method=subscription.encrypted_payment_method,
                receipt_contact=self._settings.yookassa_receipt_email or None,
            )
        if current_period_end > datetime.now(UTC):
            if not provider_payment_id:
                raise PermanentProviderError("subscription_payment_missing")
            return await self._gateway.fetch_subscription_event(
                "payment.reconciliation", provider_payment_id
            )
        return await self.renew_subscription(renewal)

    async def cancel_subscription(self, subscription_id: str) -> SubscriptionStateFact:
        return await self._state(subscription_id, cancel_at_period_end=True)

    async def resume_subscription(self, subscription_id: str) -> SubscriptionStateFact:
        return await self._state(subscription_id, cancel_at_period_end=False)

    async def _state(
        self, subscription_id: str, *, cancel_at_period_end: bool
    ) -> SubscriptionStateFact:
        async with self._sessions() as session:
            subscription = await session.scalar(
                select(Subscription).where(
                    Subscription.provider == "yookassa",
                    Subscription.provider_subscription_id == subscription_id,
                )
            )
            if subscription is None:
                raise PermanentProviderError("subscription_not_found")
            return SubscriptionStateFact(
                user_id=subscription.user_id,
                provider="yookassa",
                provider_subscription_id=subscription.provider_subscription_id,
                status="active",
                current_period_start=subscription.current_period_start,
                current_period_end=subscription.current_period_end,
                cancel_at_period_end=cancel_at_period_end,
                canceled_at=None,
            )
