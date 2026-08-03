"""Real PostgreSQL webhook duplicate and mismatch races."""

import asyncio

import pytest
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.db.models import BillingJob, CreditTransaction, PaymentOrder, ProviderWebhookEvent
from app.services.payment_completion_service import PaymentCompletionService
from app.services.webhook_inbox_service import WebhookInboxService
from tests.payment_postgres_helpers import create_order, paid

pytestmark = pytest.mark.postgres


async def test_ten_duplicate_deliveries_create_one_event_and_job(
    payment_db: async_sessionmaker[AsyncSession],
) -> None:
    inbox = WebhookInboxService(payment_db)
    await asyncio.gather(
        *(
            inbox.accept(
                "stripe",
                "evt-duplicate",
                "checkout.session.completed",
                "checkout-duplicate",
                "a" * 64,
            )
            for _ in range(10)
        )
    )
    async with payment_db() as session:
        events = await session.scalar(select(func.count()).select_from(ProviderWebhookEvent))
        jobs = await session.scalar(select(func.count()).select_from(BillingJob))
    assert events == 1 and jobs == 1


async def test_changed_hash_blocks_claimed_financial_completion(
    payment_db: async_sessionmaker[AsyncSession],
) -> None:
    _, order_id = await create_order(payment_db, checkout_id="checkout-mismatch")
    inbox = WebhookInboxService(payment_db)
    event = await inbox.accept(
        "stripe", "evt-mismatch", "checkout.session.completed", "checkout-mismatch", "a" * 64
    )
    async with payment_db.begin() as session:
        job = await session.scalar(select(BillingJob).where(BillingJob.object_id == str(event.id)))
        assert job is not None
        from datetime import UTC, datetime, timedelta
        from uuid import uuid4

        claim = uuid4()
        job.status = "claimed"
        job.claim_id = claim
        job.lease_until = datetime.now(UTC) + timedelta(minutes=1)
        job.claimed_by = "worker"
    await inbox.accept(
        "stripe", "evt-mismatch", "checkout.session.completed", "checkout-mismatch", "b" * 64
    )
    outcome = await PaymentCompletionService(payment_db).complete_claimed(
        job.id, claim, order_id, paid(order_id, "checkout-mismatch")
    )
    async with payment_db() as session:
        order = await session.get(PaymentOrder, order_id)
        ledger = await session.scalar(
            select(func.count())
            .select_from(CreditTransaction)
            .where(CreditTransaction.payment_order_id == order_id)
        )
        current_event = await session.get(ProviderWebhookEvent, event.id)
        current_job = await session.get(BillingJob, job.id)
    assert outcome == "manual_review" and ledger == 0
    assert order is not None and order.status == "manual_review"
    assert current_event is not None and current_event.status == "manual_review"
    assert current_job is not None and current_job.status == "manual_review"
