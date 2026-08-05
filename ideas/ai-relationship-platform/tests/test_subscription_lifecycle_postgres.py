"""PostgreSQL concurrency regressions for durable subscription periods."""

import asyncio
from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

import pytest
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.db.models import (
    BillingJob,
    BillingOutboxEvent,
    CreditTransaction,
    PaymentOrder,
    Subscription,
    User,
)
from app.db.subscription_models import SubscriptionPeriod
from app.services.subscription_lifecycle import (
    CancellationOutcome,
    PaidSubscriptionPeriod,
    PastDueSubscriptionPeriod,
    PeriodApplyOutcome,
    SubscriptionLifecycleService,
    SubscriptionStateMismatch,
)
from tests.payment_postgres_helpers import payment_db  # noqa: F401

pytestmark = pytest.mark.postgres


async def _user(sessions: async_sessionmaker[AsyncSession]) -> UUID:
    async with sessions.begin() as session:
        user = User(telegram_user_id=uuid4().int % 10**12, first_name="Subscriber")
        session.add(user)
        await session.flush()
        return user.id


def _paid(
    *,
    invoice_id: str = "invoice-2026-08",
    payment_id: str = "payment-2026-08",
    period_start: datetime | None = None,
    period_end: datetime | None = None,
) -> PaidSubscriptionPeriod:
    start = period_start or datetime(2026, 8, 1, tzinfo=UTC)
    end = period_end or datetime(2026, 9, 1, tzinfo=UTC)
    return PaidSubscriptionPeriod(
        provider="stripe",
        provider_customer_id="cus-one",
        provider_subscription_id="sub-one",
        provider_invoice_id=invoice_id,
        provider_payment_id=payment_id,
        product_code="subscription_monthly",
        product_version=1,
        market="INTERNATIONAL",
        currency="EUR",
        amount_minor=990,
        credits=30,
        price_reference="price_subscription_eur",
        period_start=start,
        period_end=end,
        paid_at=start,
        consent_version="billing-v1",
        live_mode=False,
    )


@pytest.mark.asyncio
async def test_ten_concurrent_period_completions_grant_once(
    payment_db: async_sessionmaker[AsyncSession],  # noqa: F811
) -> None:
    user_id = await _user(payment_db)
    service = SubscriptionLifecycleService(payment_db)

    outcomes = await asyncio.gather(
        *(service.apply_paid_period(user_id, _paid()) for _ in range(10))
    )

    assert outcomes.count(PeriodApplyOutcome.APPLIED) == 1
    assert outcomes.count(PeriodApplyOutcome.ALREADY_APPLIED) == 9
    async with payment_db() as session:
        assert await session.scalar(select(func.count()).select_from(Subscription)) == 1
        assert await session.scalar(select(func.count()).select_from(SubscriptionPeriod)) == 1
        assert await session.scalar(select(func.count()).select_from(PaymentOrder)) == 1
        purchases = await session.scalars(
            select(CreditTransaction).where(CreditTransaction.type == "purchase")
        )
        purchase_rows = list(purchases)
        assert len(purchase_rows) == 1
        assert purchase_rows[0].amount == 30
        assert await session.scalar(select(func.count()).select_from(BillingOutboxEvent)) == 1


@pytest.mark.asyncio
async def test_duplicate_period_with_different_payment_is_rejected(
    payment_db: async_sessionmaker[AsyncSession],  # noqa: F811
) -> None:
    user_id = await _user(payment_db)
    service = SubscriptionLifecycleService(payment_db)
    assert await service.apply_paid_period(user_id, _paid()) is PeriodApplyOutcome.APPLIED

    with pytest.raises(SubscriptionStateMismatch):
        await service.apply_paid_period(
            user_id,
            _paid(invoice_id="invoice-conflict", payment_id="payment-conflict"),
        )

    async with payment_db() as session:
        assert await session.scalar(select(func.count()).select_from(SubscriptionPeriod)) == 1
        assert await session.scalar(select(func.count()).select_from(CreditTransaction)) == 1


@pytest.mark.asyncio
async def test_past_due_period_can_recover_to_paid_once(
    payment_db: async_sessionmaker[AsyncSession],  # noqa: F811
) -> None:
    user_id = await _user(payment_db)
    service = SubscriptionLifecycleService(payment_db)
    first = _paid(
        invoice_id="invoice-july",
        payment_id="payment-july",
        period_start=datetime(2026, 7, 1, tzinfo=UTC),
        period_end=datetime(2026, 8, 1, tzinfo=UTC),
    )
    assert await service.apply_paid_period(user_id, first) is PeriodApplyOutcome.APPLIED

    due = PastDueSubscriptionPeriod(
        provider="stripe",
        provider_subscription_id="sub-one",
        provider_invoice_id="invoice-august",
        product_code="subscription_monthly",
        product_version=1,
        currency="EUR",
        amount_minor=990,
        credits=30,
        period_start=datetime(2026, 8, 1, tzinfo=UTC),
        period_end=datetime(2026, 9, 1, tzinfo=UTC),
    )
    assert await service.mark_past_due(due)
    assert not await service.mark_past_due(due)

    recovered = _paid(invoice_id="invoice-august", payment_id="payment-august")
    assert await service.apply_paid_period(user_id, recovered) is PeriodApplyOutcome.APPLIED
    assert await service.apply_paid_period(user_id, recovered) is PeriodApplyOutcome.ALREADY_APPLIED

    async with payment_db() as session:
        subscription = await session.scalar(select(Subscription))
        periods = list(
            await session.scalars(
                select(SubscriptionPeriod).order_by(SubscriptionPeriod.period_start)
            )
        )
        assert subscription is not None and subscription.status == "active"
        assert [period.status for period in periods] == ["paid", "paid"]
        assert await session.scalar(select(func.sum(CreditTransaction.amount))) == 60


@pytest.mark.asyncio
async def test_concurrent_scheduler_creates_one_period_job(
    payment_db: async_sessionmaker[AsyncSession],  # noqa: F811
) -> None:
    user_id = await _user(payment_db)
    service = SubscriptionLifecycleService(payment_db)
    assert await service.apply_paid_period(user_id, _paid()) is PeriodApplyOutcome.APPLIED

    now = datetime(2026, 9, 1, tzinfo=UTC)
    counts = await asyncio.gather(
        *(service.enqueue_due_renewals(now=now, lookahead=timedelta()) for _ in range(10))
    )

    assert sum(counts) == 1
    async with payment_db() as session:
        jobs = list(await session.scalars(select(BillingJob)))
        assert len(jobs) == 1
        assert jobs[0].job_type == "subscription_renewal"
        assert jobs[0].object_type == "subscription"


@pytest.mark.asyncio
async def test_cancel_at_period_end_preserves_credits_and_finalizes_once(
    payment_db: async_sessionmaker[AsyncSession],  # noqa: F811
) -> None:
    user_id = await _user(payment_db)
    service = SubscriptionLifecycleService(payment_db)
    paid = _paid()
    assert await service.apply_paid_period(user_id, paid) is PeriodApplyOutcome.APPLIED

    async with payment_db() as session:
        subscription_id = await session.scalar(select(Subscription.id))
    assert subscription_id is not None

    assert (
        await service.record_cancel_at_period_end(user_id, subscription_id, paid.period_end)
        is CancellationOutcome.UPDATED
    )
    assert (
        await service.record_cancel_at_period_end(user_id, subscription_id, paid.period_end)
        is CancellationOutcome.ALREADY_TERMINAL
    )

    canceled, unpaid = await service.finalize_terminal_states(
        now=paid.period_end + timedelta(seconds=1)
    )
    assert (canceled, unpaid) == (1, 0)
    assert await service.finalize_terminal_states(now=paid.period_end + timedelta(days=1)) == (
        0,
        0,
    )

    async with payment_db() as session:
        subscription = await session.get(Subscription, subscription_id)
        assert subscription is not None and subscription.status == "canceled"
        assert await session.scalar(select(func.sum(CreditTransaction.amount))) == 30


@pytest.mark.asyncio
async def test_resume_before_period_end_is_idempotent(
    payment_db: async_sessionmaker[AsyncSession],  # noqa: F811
) -> None:
    user_id = await _user(payment_db)
    service = SubscriptionLifecycleService(payment_db)
    paid = _paid()
    await service.apply_paid_period(user_id, paid)
    async with payment_db() as session:
        subscription_id = await session.scalar(select(Subscription.id))
    assert subscription_id is not None

    await service.record_cancel_at_period_end(user_id, subscription_id, paid.period_end)
    assert (
        await service.record_resumed(
            user_id,
            subscription_id,
            now=paid.period_end - timedelta(days=1),
        )
        is CancellationOutcome.UPDATED
    )
    assert (
        await service.record_resumed(
            user_id,
            subscription_id,
            now=paid.period_end - timedelta(hours=1),
        )
        is CancellationOutcome.ALREADY_TERMINAL
    )


@pytest.mark.asyncio
async def test_past_due_expires_after_grace_without_credit_mutation(
    payment_db: async_sessionmaker[AsyncSession],  # noqa: F811
) -> None:
    user_id = await _user(payment_db)
    service = SubscriptionLifecycleService(payment_db)
    first = _paid(
        invoice_id="invoice-july",
        payment_id="payment-july",
        period_start=datetime(2026, 7, 1, tzinfo=UTC),
        period_end=datetime(2026, 8, 1, tzinfo=UTC),
    )
    await service.apply_paid_period(user_id, first)
    due = PastDueSubscriptionPeriod(
        provider="stripe",
        provider_subscription_id="sub-one",
        provider_invoice_id="invoice-august",
        product_code="subscription_monthly",
        product_version=1,
        currency="EUR",
        amount_minor=990,
        credits=30,
        period_start=datetime(2026, 8, 1, tzinfo=UTC),
        period_end=datetime(2026, 9, 1, tzinfo=UTC),
    )
    await service.mark_past_due(due)

    assert await service.finalize_terminal_states(
        now=datetime(2026, 9, 4, tzinfo=UTC),
        grace_period=timedelta(days=3),
    ) == (0, 1)
    async with payment_db() as session:
        subscription = await session.scalar(select(Subscription))
        assert subscription is not None and subscription.status == "unpaid"
        assert await session.scalar(select(func.sum(CreditTransaction.amount))) == 30
