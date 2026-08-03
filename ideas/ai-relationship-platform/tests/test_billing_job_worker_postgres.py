"""Real PostgreSQL billing worker claim takeover and resilience."""

from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.db.models import BillingJob, BillingOutboxEvent, CreditTransaction, PaymentOrder
from app.services.billing_job_worker import BillingJobWorker
from app.services.payment_completion_service import PaymentCompletionService
from tests.payment_postgres_helpers import (
    FakeGateway,
    create_claimed_job,
    create_order,
    paid,
)

pytestmark = pytest.mark.postgres
pytest_plugins = ("tests.payment_postgres_helpers",)


async def test_stale_claim_cannot_complete_but_replacement_completes_once(
    payment_db: async_sessionmaker[AsyncSession],
) -> None:
    _, order_id = await create_order(payment_db, checkout_id="checkout-takeover")
    job_id, claim_a, _ = await create_claimed_job(payment_db, order_id)
    async with payment_db.begin() as session:
        job = await session.get(BillingJob, job_id)
        assert job is not None
        job.lease_until = datetime.now(UTC) - timedelta(seconds=1)
    worker = BillingJobWorker(payment_db, {}, PaymentCompletionService(payment_db))
    reclaimed = await worker.claim_one("worker-b")
    assert reclaimed is not None and reclaimed[0] == job_id
    claim_b = reclaimed[1]
    service = PaymentCompletionService(payment_db)
    assert (
        await service.complete_claimed(
            job_id, claim_a, order_id, paid(order_id, "checkout-takeover")
        )
        == "claim_lost"
    )
    async with payment_db() as session:
        before = await session.scalar(select(func.count()).select_from(CreditTransaction))
    assert before == 0
    assert (
        await service.complete_claimed(
            job_id, claim_b, order_id, paid(order_id, "checkout-takeover")
        )
        == "completed"
    )
    async with payment_db() as session:
        ledger = await session.scalar(select(func.count()).select_from(CreditTransaction))
        outbox = await session.scalar(select(func.count()).select_from(BillingOutboxEvent))
    assert ledger == 1 and outbox == 1


async def test_unsupported_job_manual_review_then_valid_job_runs(
    payment_db: async_sessionmaker[AsyncSession],
) -> None:
    _, bad_order = await create_order(payment_db, provider="disabled")
    _, good_order = await create_order(payment_db, checkout_id="checkout-good")
    bad = await create_claimed_job(payment_db, bad_order)
    good = await create_claimed_job(payment_db, good_order)
    async with payment_db.begin() as session:
        for job_id in (bad[0], good[0]):
            job = await session.get(BillingJob, job_id)
            assert job is not None
            job.status = "pending"
            job.claim_id = None
            job.lease_until = None
    gateway = FakeGateway(paid(good_order, "checkout-good"))
    worker = BillingJobWorker(payment_db, {"stripe": gateway}, PaymentCompletionService(payment_db))
    assert await worker.run_once("worker")
    assert await worker.run_once("worker")
    async with payment_db() as session:
        bad_row = await session.get(PaymentOrder, bad_order)
        good_row = await session.get(PaymentOrder, good_order)
    assert bad_row is not None and bad_row.status == "manual_review"
    assert good_row is not None and good_row.status == "completed"
