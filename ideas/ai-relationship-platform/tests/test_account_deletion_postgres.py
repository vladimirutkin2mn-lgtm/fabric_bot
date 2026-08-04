"""Account deletion billing isolation on real PostgreSQL."""

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
    ProviderWebhookEvent,
    User,
)
from app.providers.analytics import NoOpAnalyticsClient
from app.services.data_deletion import DataDeletionOutcome, DataDeletionService
from app.services.payment_completion_service import PaymentCompletionService
from tests.payment_postgres_helpers import create_claimed_job, create_order, paid

pytestmark = pytest.mark.postgres


async def test_webhook_cleanup_matches_provider_and_object_id(
    payment_db: async_sessionmaker[AsyncSession],
) -> None:
    shared = "same-provider-object-id"
    async with payment_db.begin() as session:
        users = [User(telegram_user_id=88001 + index, first_name="Test") for index in range(2)]
        session.add_all(users)
        await session.flush()
        jobs: list[BillingJob] = []
        for user, provider in zip(users, ("stripe", "yookassa"), strict=True):
            order = PaymentOrder(
                user_id=user.id,
                provider=provider,
                product_code="analysis_single",
                status="pending",
                credits=1,
                amount_minor=500,
                currency="RUB" if provider == "yookassa" else "EUR",
                market="RU" if provider == "yookassa" else "INTERNATIONAL",
                provider_checkout_id=shared,
                idempotency_key=f"checkout:{uuid4()}",
                commercial_snapshot={},
            )
            event = ProviderWebhookEvent(
                provider=provider,
                provider_event_id=f"event-{uuid4()}",
                event_type="payment.completed",
                provider_object_id=shared,
                payload_hash="a" * 64,
                status="processing",
            )
            session.add_all((order, event))
            await session.flush()
            job = BillingJob(
                job_type="webhook_processing",
                provider=provider,
                object_type="webhook_event",
                object_id=str(event.id),
                idempotency_key=f"job:{uuid4()}",
                status="claimed",
                claimed_by="worker",
                claim_id=uuid4(),
                claimed_at=datetime.now(UTC),
                lease_until=datetime.now(UTC) + timedelta(minutes=5),
            )
            session.add(job)
            jobs.append(job)
        await session.flush()
        deleted_user_id, stripe_job_id, yookassa_job_id = users[0].id, jobs[0].id, jobs[1].id
    async with payment_db() as session:
        outcome = await DataDeletionService(session, NoOpAnalyticsClient()).delete_account(
            deleted_user_id
        )
        assert outcome is DataDeletionOutcome.DELETED
    async with payment_db() as session:
        stripe_job = await session.get(BillingJob, stripe_job_id)
        yookassa_job = await session.get(BillingJob, yookassa_job_id)
        assert stripe_job is not None and stripe_job.status == "manual_review"
        assert stripe_job.last_error_code == "user_deleted" and stripe_job.claim_id is None
        assert yookassa_job is not None and yookassa_job.status == "claimed"
        assert yookassa_job.last_error_code is None and yookassa_job.claim_id is not None


async def test_payment_completion_and_account_deletion_race_25_times(
    payment_db: async_sessionmaker[AsyncSession],
) -> None:
    for iteration in range(25):
        checkout = f"privacy-race-checkout-{iteration}"
        user_id, order_id = await create_order(payment_db, checkout_id=checkout)
        job_id, claim_id, _ = await create_claimed_job(payment_db, order_id)

        async def delete(target_user_id: UUID = user_id) -> DataDeletionOutcome:
            async with payment_db() as session:
                return await DataDeletionService(session, NoOpAnalyticsClient()).delete_account(
                    target_user_id
                )

        completion, deletion = await asyncio.gather(
            PaymentCompletionService(payment_db).complete_claimed(
                job_id,
                claim_id,
                order_id,
                paid(order_id, checkout, f"privacy-race-payment-{iteration}"),
            ),
            delete(),
        )
        assert deletion is DataDeletionOutcome.DELETED
        assert completion in {"completed", "user_deleted", "claim_lost", "already_cancelled"}
        async with payment_db() as session:
            user = await session.get(User, user_id)
            order = await session.get(PaymentOrder, order_id)
            job = await session.get(BillingJob, job_id)
            purchase_count = await session.scalar(
                select(func.count())
                .select_from(CreditTransaction)
                .where(CreditTransaction.payment_order_id == order_id)
            )
            outbox_count = await session.scalar(
                select(func.count())
                .select_from(BillingOutboxEvent)
                .where(BillingOutboxEvent.idempotency_key == f"purchase_completed:{order_id}")
            )
        assert user is not None and user.privacy_status == "deleted"
        assert order is not None and order.status in {"completed", "cancelled"}
        assert job is not None and job.status in {"completed", "manual_review"}
        assert purchase_count in {0, 1} and outbox_count == purchase_count
        if completion == "completed":
            assert purchase_count == 1 and order.status == "completed"
        else:
            assert purchase_count == 0
