"""Apply provider-authoritative subscription facts to the durable lifecycle."""

from datetime import UTC, datetime, timedelta

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.db.models import BillingOutboxEvent, PaymentOrder, Subscription, User
from app.providers.payments.subscription_gateway import (
    InitialSubscriptionFailedFact,
    PaidSubscriptionFact,
    PastDueSubscriptionFact,
    SubscriptionProviderFact,
    SubscriptionStateFact,
)
from app.services.subscription_lifecycle import (
    PaidSubscriptionPeriod,
    PastDueSubscriptionPeriod,
    SubscriptionLifecycleService,
    SubscriptionStateMismatch,
)


class SubscriptionEventProcessor:
    def __init__(
        self,
        sessions: async_sessionmaker[AsyncSession],
        lifecycle: SubscriptionLifecycleService,
        grace_period_days: int,
    ) -> None:
        self._sessions = sessions
        self._lifecycle = lifecycle
        self._grace = timedelta(days=grace_period_days)

    async def apply(self, fact: SubscriptionProviderFact) -> bool:
        if isinstance(fact, PaidSubscriptionFact):
            await self._lifecycle.apply_paid_period(
                fact.user_id,
                PaidSubscriptionPeriod(
                    provider=fact.provider,
                    provider_customer_id=fact.provider_customer_id,
                    provider_subscription_id=fact.provider_subscription_id,
                    provider_invoice_id=fact.provider_invoice_id,
                    provider_payment_id=fact.provider_payment_id,
                    product_code=fact.product_code,
                    product_version=fact.product_version,
                    market=fact.market,
                    currency=fact.currency,
                    amount_minor=fact.amount_minor,
                    credits=fact.credits,
                    price_reference=fact.price_reference,
                    period_start=fact.period_start,
                    period_end=fact.period_end,
                    paid_at=fact.paid_at,
                    consent_version=fact.consent_version,
                    initial_order_id=fact.initial_order_id,
                    live_mode=fact.live_mode,
                ),
            )
            if fact.encrypted_payment_method is not None:
                await self._store_payment_method(fact)
            return True
        if isinstance(fact, PastDueSubscriptionFact):
            return await self._lifecycle.mark_past_due(
                PastDueSubscriptionPeriod(
                    provider=fact.provider,
                    provider_subscription_id=fact.provider_subscription_id,
                    provider_invoice_id=fact.provider_invoice_id,
                    product_code=fact.product_code,
                    product_version=fact.product_version,
                    currency=fact.currency,
                    amount_minor=fact.amount_minor,
                    credits=fact.credits,
                    period_start=fact.period_start,
                    period_end=fact.period_end,
                )
            )
        if isinstance(fact, InitialSubscriptionFailedFact):
            return await self._fail_initial_order(fact)
        return await self._apply_state(fact)

    async def _store_payment_method(self, fact: PaidSubscriptionFact) -> None:
        async with self._sessions.begin() as session:
            user = await session.scalar(
                select(User).where(User.id == fact.user_id).with_for_update()
            )
            subscription = await session.scalar(
                select(Subscription)
                .where(
                    Subscription.provider == fact.provider,
                    Subscription.provider_subscription_id == fact.provider_subscription_id,
                )
                .with_for_update()
            )
            if (
                user is None
                or subscription is None
                or subscription.user_id != fact.user_id
                or user.privacy_status != "active"
            ):
                raise SubscriptionStateMismatch("saved payment method owner mismatch")
            if subscription.encrypted_payment_method is None:
                subscription.encrypted_payment_method = fact.encrypted_payment_method

    async def _fail_initial_order(self, fact: InitialSubscriptionFailedFact) -> bool:
        async with self._sessions.begin() as session:
            user = await session.scalar(
                select(User).where(User.id == fact.user_id).with_for_update()
            )
            order = await session.scalar(
                select(PaymentOrder).where(PaymentOrder.id == fact.order_id).with_for_update()
            )
            if order is None or order.user_id != fact.user_id:
                return False
            if order.provider != fact.provider or order.mode != "subscription_initial":
                raise SubscriptionStateMismatch("initial subscription order identity mismatch")
            if order.status == "completed":
                raise SubscriptionStateMismatch("completed initial subscription cannot fail")
            if order.status in {"failed", "cancelled"}:
                return False
            if user is None or user.privacy_status != "active":
                order.status = "cancelled"
                order.failure_code = "user_deleted"
            else:
                order.status = "cancelled"
                order.failure_code = "initial_subscription_payment_canceled"
            order.provider_payment_id = fact.provider_payment_id
            order.provider_status = fact.provider_status
            order.checkout_url = None
            order.checkout_creation_attempt_id = None
            order.checkout_creation_started_at = None
            session.add(
                BillingOutboxEvent(
                    aggregate_type="payment_order",
                    aggregate_id=str(order.id),
                    event_type="payment_failed",
                    payload={"product_code": order.product_code, "provider": order.provider},
                    idempotency_key=f"subscription_initial_failed:{order.id}",
                )
            )
            return True

    async def _apply_state(self, fact: SubscriptionStateFact) -> bool:
        async with self._sessions() as session:
            subscription = await session.scalar(
                select(Subscription).where(
                    Subscription.provider == fact.provider,
                    Subscription.provider_subscription_id == fact.provider_subscription_id,
                )
            )
            if subscription is None:
                return False
            if subscription.user_id != fact.user_id:
                raise SubscriptionStateMismatch("provider subscription owner mismatch")
            subscription_id = subscription.id
            stored_status = subscription.status

        effective = fact.current_period_end or fact.canceled_at
        canceled = fact.status in {"canceled", "unpaid", "incomplete_expired"}
        if fact.cancel_at_period_end or canceled:
            if effective is None:
                raise SubscriptionStateMismatch("provider cancellation boundary missing")
            await self._lifecycle.record_cancel_at_period_end(
                fact.user_id, subscription_id, effective
            )
            if effective <= datetime.now(UTC):
                await self._lifecycle.finalize_terminal_states(
                    now=datetime.now(UTC), grace_period=self._grace
                )
            return True
        if fact.status in {"active", "trialing"} and stored_status == "cancel_at_period_end":
            await self._lifecycle.record_resumed(fact.user_id, subscription_id)
            return True
        return False
