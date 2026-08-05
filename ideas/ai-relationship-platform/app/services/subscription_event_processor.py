"""Apply provider-authoritative subscription facts to the durable lifecycle."""

from datetime import UTC, datetime, timedelta

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.db.models import Subscription
from app.providers.payments.subscription_gateway import (
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
                    live_mode=fact.live_mode,
                ),
            )
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
        return await self._apply_state(fact)

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
