"""YooKassa initial payment stores an encrypted method without duplicate credits."""

from datetime import UTC, datetime
from uuid import UUID, uuid4

import pytest
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.db.models import CreditTransaction, PaymentOrder, Subscription, User
from app.providers.payments.subscription_gateway import PaidSubscriptionFact
from app.services.subscription_event_processor import SubscriptionEventProcessor
from app.services.subscription_lifecycle import SubscriptionLifecycleService
from tests.payment_postgres_helpers import payment_db  # noqa: F401

pytestmark = pytest.mark.postgres


async def _checkout_order(sessions: async_sessionmaker[AsyncSession]) -> tuple[UUID, UUID]:
    async with sessions.begin() as session:
        user = User(telegram_user_id=uuid4().int % 10**12, first_name="YooKassa")
        session.add(user)
        await session.flush()
        order = PaymentOrder(
            user_id=user.id,
            provider="yookassa",
            product_code="subscription_monthly",
            status="pending",
            credits=30,
            amount_minor=99_000,
            currency="RUB",
            mode="subscription_initial",
            market="RU",
            product_version=1,
            billing_period="month",
            provider_checkout_id="payment-initial",
            provider_status="pending",
            idempotency_key=f"subscription:checkout:{uuid4()}:v1",
            commercial_snapshot={
                "product_code": "subscription_monthly",
                "product_version": 1,
                "credits": 30,
                "amount_minor": 99_000,
                "currency": "RUB",
                "provider": "yookassa",
                "market": "RU",
                "price_reference": "catalog:subscription_monthly:rub:v1",
                "billing_period": "month",
                "consent_version": "billing-v1",
            },
        )
        session.add(order)
        await session.flush()
        return user.id, order.id


def _paid(user_id: UUID, order_id: UUID) -> PaidSubscriptionFact:
    return PaidSubscriptionFact(
        user_id=user_id,
        initial_order_id=order_id,
        provider="yookassa",
        provider_customer_id=f"yookassa:{user_id}",
        provider_subscription_id=f"yookassa:{order_id}",
        provider_invoice_id="payment-initial",
        provider_payment_id="payment-initial",
        product_code="subscription_monthly",
        product_version=1,
        market="RU",
        currency="RUB",
        amount_minor=99_000,
        credits=30,
        price_reference="catalog:subscription_monthly:rub:v1",
        period_start=datetime(2026, 8, 5, 8, tzinfo=UTC),
        period_end=datetime(2026, 9, 5, 8, tzinfo=UTC),
        paid_at=datetime(2026, 8, 5, 8, 1, tzinfo=UTC),
        consent_version="billing-v1",
        live_mode=False,
        encrypted_payment_method=b"authenticated-encrypted-envelope",
    )


@pytest.mark.asyncio
async def test_duplicate_initial_fact_stores_method_and_grants_once(
    payment_db: async_sessionmaker[AsyncSession],  # noqa: F811
) -> None:
    user_id, order_id = await _checkout_order(payment_db)
    processor = SubscriptionEventProcessor(
        payment_db,
        SubscriptionLifecycleService(payment_db),
        grace_period_days=3,
    )
    fact = _paid(user_id, order_id)

    assert await processor.apply(fact) is True
    assert await processor.apply(fact) is True

    async with payment_db() as session:
        order = await session.get(PaymentOrder, order_id)
        subscription = await session.scalar(select(Subscription))
        assert order is not None and subscription is not None
        assert order.status == "completed"
        assert order.subscription_id == subscription.id
        assert subscription.encrypted_payment_method == fact.encrypted_payment_method
        assert await session.scalar(select(func.count()).select_from(PaymentOrder)) == 1
        assert await session.scalar(select(func.count()).select_from(CreditTransaction)) == 1
        transaction = await session.scalar(select(CreditTransaction))
        assert transaction is not None
        assert transaction.payment_order_id == order_id
        assert transaction.amount == 30
