"""Real PostgreSQL exactly-once payment completion races."""

import asyncio

import pytest
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.db.models import BillingJob, BillingOutboxEvent, CreditTransaction, PaymentOrder
from app.services.payment_completion_service import PaymentCompletionService
from tests.payment_postgres_helpers import create_claimed_job, create_order, paid

pytestmark = pytest.mark.postgres


async def test_ten_claimed_completions_grant_one_purchase(
    payment_db: async_sessionmaker[AsyncSession],
) -> None:
    _, order_id = await create_order(payment_db, checkout_id="checkout-race")
    claims = [await create_claimed_job(payment_db, order_id) for _ in range(10)]
    service = PaymentCompletionService(payment_db)
    outcomes = await asyncio.gather(
        *(
            service.complete_claimed(job_id, claim_id, order_id, paid(order_id, "checkout-race"))
            for job_id, claim_id, _ in claims
        )
    )
    assert outcomes.count("completed") == 1
    assert all(value in {"completed", "already_completed"} for value in outcomes)
    async with payment_db() as session:
        order = await session.get(PaymentOrder, order_id)
        purchases = await session.scalar(
            select(func.count())
            .select_from(CreditTransaction)
            .where(
                CreditTransaction.payment_order_id == order_id, CreditTransaction.type == "purchase"
            )
        )
        outbox = await session.scalar(
            select(func.count())
            .select_from(BillingOutboxEvent)
            .where(BillingOutboxEvent.idempotency_key == f"purchase_completed:{order_id}")
        )
    assert order is not None and order.status == "completed"
    assert purchases == 1 and outbox == 1


async def test_webhook_and_reconciliation_race_25_times(
    payment_db: async_sessionmaker[AsyncSession],
) -> None:
    service = PaymentCompletionService(payment_db)
    for iteration in range(25):
        checkout = f"checkout-iteration-{iteration}"
        _, order_id = await create_order(payment_db, checkout_id=checkout)
        webhook = await create_claimed_job(payment_db, order_id, webhook=True, checkout_id=checkout)
        reconcile = await create_claimed_job(payment_db, order_id)
        results = await asyncio.gather(
            service.complete_claimed(
                webhook[0], webhook[1], order_id, paid(order_id, checkout, f"pay-{iteration}")
            ),
            service.complete_claimed(
                reconcile[0], reconcile[1], order_id, paid(order_id, checkout, f"pay-{iteration}")
            ),
        )
        assert set(results) <= {"completed", "already_completed"}
        async with payment_db() as session:
            purchase_count = await session.scalar(
                select(func.count())
                .select_from(CreditTransaction)
                .where(
                    CreditTransaction.payment_order_id == order_id,
                    CreditTransaction.type == "purchase",
                )
            )
            outbox_count = await session.scalar(
                select(func.count())
                .select_from(BillingOutboxEvent)
                .where(BillingOutboxEvent.idempotency_key == f"purchase_completed:{order_id}")
            )
            jobs = list(
                await session.scalars(
                    select(BillingJob).where(BillingJob.id.in_([webhook[0], reconcile[0]]))
                )
            )
        assert purchase_count == 1 and outbox_count == 1
        assert {job.status for job in jobs} == {"completed"}


async def test_already_manual_review_keeps_job_and_event_manual(
    payment_db: async_sessionmaker[AsyncSession],
) -> None:
    _, order_id = await create_order(payment_db, checkout_id="checkout-manual")
    job_id, claim_id, event_id = await create_claimed_job(
        payment_db, order_id, webhook=True, checkout_id="checkout-manual"
    )
    async with payment_db.begin() as session:
        order = await session.get(PaymentOrder, order_id)
        assert order is not None
        order.status, order.failure_code = "manual_review", "existing_review"
    outcome = await PaymentCompletionService(payment_db).complete_claimed(
        job_id, claim_id, order_id, paid(order_id, "checkout-manual")
    )
    async with payment_db() as session:
        order = await session.get(PaymentOrder, order_id)
        job = await session.get(BillingJob, job_id)
        event = await session.get(
            __import__("app.db.models", fromlist=["ProviderWebhookEvent"]).ProviderWebhookEvent,
            event_id,
        )
    assert outcome == "already_manual_review"
    assert (
        order is not None
        and order.status == "manual_review"
        and order.failure_code == "existing_review"
    )
    assert job is not None and job.status == "manual_review"
    assert event is not None and event.status == "manual_review"


async def test_two_orders_same_payment_identity_leave_one_manual_review(
    payment_db: async_sessionmaker[AsyncSession],
) -> None:
    _, first_id = await create_order(payment_db, checkout_id="checkout-identity-a")
    _, second_id = await create_order(payment_db, checkout_id="checkout-identity-b")
    first_job = await create_claimed_job(payment_db, first_id)
    second_job = await create_claimed_job(payment_db, second_id)
    service = PaymentCompletionService(payment_db)
    results = await asyncio.gather(
        service.complete_claimed(
            first_job[0],
            first_job[1],
            first_id,
            paid(first_id, "checkout-identity-a", "shared-payment"),
        ),
        service.complete_claimed(
            second_job[0],
            second_job[1],
            second_id,
            paid(second_id, "checkout-identity-b", "shared-payment"),
        ),
    )
    assert sorted(results) == ["completed", "manual_review"]
    async with payment_db() as session:
        orders = list(
            await session.scalars(
                select(PaymentOrder).where(PaymentOrder.id.in_([first_id, second_id]))
            )
        )
        purchases = await session.scalar(
            select(func.count())
            .select_from(CreditTransaction)
            .where(CreditTransaction.payment_order_id.in_([first_id, second_id]))
        )
        outbox = await session.scalar(
            select(func.count())
            .select_from(BillingOutboxEvent)
            .where(BillingOutboxEvent.event_type == "purchase_completed")
        )
    assert {order.status for order in orders} == {"completed", "manual_review"}
    reviewed = next(order for order in orders if order.status == "manual_review")
    assert reviewed.failure_code == "payment_identity_reused"
    assert purchases == 1 and outbox == 1
