"""Account deletion billing isolation on real PostgreSQL."""

from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.db.models import BillingJob, PaymentOrder, ProviderWebhookEvent, User
from app.providers.analytics import NoOpAnalyticsClient
from app.services.data_deletion import DataDeletionOutcome, DataDeletionService

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
