"""Initial Stripe checkout and first paid period must share one financial order."""

from datetime import UTC, datetime
from uuid import UUID, uuid4

import pytest
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.db.models import CreditTransaction, PaymentOrder, Subscription, User
from app.services.subscription_lifecycle import (
    PaidSubscriptionPeriod,
    PeriodApplyOutcome,
    SubscriptionLifecycleService,
)
from tests.payment_postgres_helpers import payment_db  # noqa: F401

pytestmark = pytest.mark.postgres


async def _checkout_order(sessions: async_sessionmaker[AsyncSession]) -> tuple[UUID, UUID]:
    async with sessions.begin() as session:
        user = User(telegram_user_id=uuid4().int % 10**12, first_name="Initial")
        session.add(user)
        await session.flush()
        order = PaymentOrder(
            user_id=user.id,
            provider="stripe",
            product_code="subscription_monthly",
            status="pending",
            credits=30,
            amount_minor=990,
            currency="EUR",
            mode="subscription_initial",
            market="INTERNATIONAL",
            product_version=1,
            billing_period="month",
            provider_checkout_id="cs_initial",
            provider_status="open",
            idempotency_key=f"subscription:checkout:{uuid4()}:v1",
            commercial_snapshot={
                "product_code": "subscription_monthly",
                "product_version": 1,
                "credits": 30,
                "amount_minor": 990,
                "currency": "EUR",
                "provider": "stripe",
                "market": "INTERNATIONAL",
                "price_reference": "price_subscription_eur",
                "billing_period": "month",
                "consent_version": "billing-v1",
            },
        )
        session.add(order)
        await session.flush()
        return user.id, order.id


def _paid(order_id: UUID) -> PaidSubscriptionPeriod:
    return PaidSubscriptionPeriod(
        provider="stripe",
        provider_customer_id="cus-initial",
        provider_subscription_id="sub-initial",
        provider_invoice_id="in-initial",
        provider_payment_id="pi-initial",
        product_code="subscription_monthly",
        product_version=1,
        market="INTERNATIONAL",
        currency="EUR",
        amount_minor=990,
        credits=30,
        price_reference="price_subscription_eur",
        period_start=datetime(2026, 8, 1, tzinfo=UTC),
        period_end=datetime(2026, 9, 1, tzinfo=UTC),
        paid_at=datetime(2026, 8, 1, 0, 1, tzinfo=UTC),
        consent_version="billing-v1",
        initial_order_id=order_id,
        live_mode=False,
    )


@pytest.mark.asyncio
async def test_first_paid_period_completes_existing_checkout_order_once(
    payment_db: async_sessionmaker[AsyncSession],  # noqa: F811
) -> None:
    user_id, order_id = await _checkout_order(payment_db)
    service = SubscriptionLifecycleService(payment_db)
    paid = _paid(order_id)

    assert await service.apply_paid_period(user_id, paid) is PeriodApplyOutcome.APPLIED
    assert (
        await service.apply_paid_period(user_id, paid)
        is PeriodApplyOutcome.ALREADY_APPLIED
    )

    async with payment_db() as session:
        assert await session.scalar(select(func.count()).select_from(PaymentOrder)) == 1
        order = await session.get(PaymentOrder, order_id)
        subscription = await session.scalar(select(Subscription))
        transaction = await session.scalar(select(CreditTransaction))
        assert order is not None and subscription is not None and transaction is not None
        assert order.status == "completed"
        assert order.provider_checkout_id == "cs_initial"
        assert order.provider_invoice_id == "in-initial"
        assert order.provider_payment_id == "pi-initial"
        assert order.subscription_id == subscription.id
        assert order.billing_period != "month"
        assert order.commercial_snapshot["subscription_id"] == str(subscription.id)
        assert transaction.payment_order_id == order_id
        assert transaction.amount == 30
