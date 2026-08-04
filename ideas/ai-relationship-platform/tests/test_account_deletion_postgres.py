"""Account deletion billing isolation on real PostgreSQL."""

import asyncio
from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

import pytest
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.db.models import (
    Analysis,
    AnalysisPrivateContent,
    BillingCustomer,
    BillingJob,
    BillingOutboxEvent,
    CreditTransaction,
    PaymentOrder,
    ProviderWebhookEvent,
    Subscription,
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


async def test_complete_account_tombstone_preserves_immutable_ledger(
    payment_db: async_sessionmaker[AsyncSession],
) -> None:
    sentinel = "PRIVATE-TOMBSTONE-SENTINEL"
    now = datetime.now(UTC)
    async with payment_db.begin() as session:
        user = User(
            telegram_user_id=88100,
            telegram_username="private_username",
            first_name="Private",
            telegram_language="ru",
            age_confirmed=True,
            age_confirmed_at=now,
            consent_version="privacy-v1",
            consent_accepted_at=now,
            onboarding_completed=True,
        )
        unrelated = User(telegram_user_id=88101, first_name="Unrelated")
        session.add_all((user, unrelated))
        await session.flush()
        analyses: list[Analysis] = []
        for status in ("draft", "processing", "completed", "failed"):
            analysis = Analysis(
                user_id=user.id,
                status=status,
                intake_step="complete",
                normalized_conversation_json=[{"text": sentinel}],
                participants_json={"A": sentinel, "B": "Other"},
                user_participant_label="A",
                user_goal=sentinel,
                relationship_stage="dating",
                message_count=4,
                character_count=len(sentinel),
                result_json={"private": sentinel} if status == "completed" else None,
                completed_at=now if status == "completed" else None,
                failure_code="safe" if status == "failed" else None,
                report_access="full" if status == "completed" else "none",
                feedback_score=5 if status == "completed" else None,
                feedback_submitted_at=now if status == "completed" else None,
            )
            session.add(analysis)
            await session.flush()
            session.add(
                AnalysisPrivateContent(
                    analysis_id=analysis.id,
                    source_ciphertext=b"private-source",
                    result_ciphertext=b"private-result",
                )
            )
            analyses.append(analysis)
        unrelated_analysis = Analysis(
            user_id=unrelated.id,
            status="draft",
            intake_step="complete",
            user_goal="unrelated-goal",
            message_count=2,
            character_count=10,
        )
        session.add(unrelated_analysis)
        customer = BillingCustomer(
            user_id=user.id, provider="stripe", provider_customer_id=sentinel
        )
        session.add(customer)
        await session.flush()
        subscription = Subscription(
            user_id=user.id,
            billing_customer_id=customer.id,
            provider="stripe",
            provider_subscription_id=f"sub-{uuid4()}",
            product_code="subscription_monthly",
            product_version=1,
            status="active",
            encrypted_payment_method=b"private-method",
            consent_version="billing-v1",
            consented_at=now,
        )
        completed_order = PaymentOrder(
            user_id=user.id,
            provider="stripe",
            product_code="analysis_single",
            status="completed",
            credits=1,
            amount_minor=500,
            currency="EUR",
            market="INTERNATIONAL",
            provider_checkout_id=f"completed-{uuid4()}",
            provider_payment_id=f"payment-{uuid4()}",
            completed_at=now,
            idempotency_key=f"completed:{uuid4()}",
            commercial_snapshot={},
        )
        pending_order = PaymentOrder(
            user_id=user.id,
            provider="stripe",
            product_code="analysis_pack_5",
            status="pending",
            credits=5,
            amount_minor=1800,
            currency="EUR",
            market="INTERNATIONAL",
            provider_checkout_id=f"pending-{uuid4()}",
            checkout_url="https://private.invalid/checkout",
            encrypted_receipt_contact=sentinel.encode(),
            idempotency_key=f"pending:{uuid4()}",
            commercial_snapshot={},
        )
        session.add_all((subscription, completed_order, pending_order))
        await session.flush()
        transaction = CreditTransaction(
            user_id=user.id,
            type="purchase",
            amount=1,
            idempotency_key=f"purchase:{completed_order.id}",
            payment_order_id=completed_order.id,
            product_code="analysis_single",
            external_payment_provider="stripe",
            external_payment_id=completed_order.provider_payment_id,
        )
        event = ProviderWebhookEvent(
            provider="stripe",
            provider_event_id=f"evt-{uuid4()}",
            event_type="payment.completed",
            provider_object_id=pending_order.provider_checkout_id or "",
            payload_hash="a" * 64,
            status="processing",
        )
        session.add_all((transaction, event))
        await session.flush()
        jobs = [
            BillingJob(
                job_type=job_type,
                provider="stripe",
                object_type=object_type,
                object_id=object_id,
                idempotency_key=f"job:{uuid4()}",
                status="claimed",
                claimed_by="worker",
                claim_id=uuid4(),
                claimed_at=now,
                lease_until=now + timedelta(minutes=5),
            )
            for job_type, object_type, object_id in (
                ("webhook_processing", "webhook_event", str(event.id)),
                ("payment_reconciliation", "payment_order", str(pending_order.id)),
            )
        ]
        session.add_all(jobs)
        await session.flush()
        user_id = user.id
        analysis_ids = [row.id for row in analyses]
        job_ids = [row.id for row in jobs]
        transaction_snapshot = (
            transaction.id,
            transaction.user_id,
            transaction.type,
            transaction.amount,
            transaction.idempotency_key,
            transaction.payment_order_id,
            transaction.external_payment_provider,
            transaction.external_payment_id,
        )
        ledger_count = await session.scalar(select(func.count()).select_from(CreditTransaction))
        unrelated_snapshot = (
            unrelated.id,
            unrelated.telegram_user_id,
            unrelated.first_name,
            unrelated.privacy_status,
            unrelated_analysis.id,
            unrelated_analysis.status,
            unrelated_analysis.user_goal,
        )
        references = (customer.id, subscription.id, pending_order.id, event.id, unrelated.id)
    async with payment_db() as session:
        assert (
            await DataDeletionService(session, NoOpAnalyticsClient()).delete_account(user_id)
            is DataDeletionOutcome.DELETED
        )
    async with payment_db() as session:
        stored_user = await session.get(User, user_id)
        assert stored_user is not None and stored_user.id == user_id
        assert stored_user.privacy_status == "deleted" and stored_user.deleted_at is not None
        assert stored_user.telegram_user_id is None and stored_user.telegram_username is None
        assert stored_user.first_name is None and stored_user.telegram_language is None
        assert stored_user.consent_version is None and stored_user.consent_accepted_at is None
        assert stored_user.age_confirmed is False and stored_user.age_confirmed_at is None
        assert stored_user.onboarding_completed is False
        assert stored_user.free_preview_status == "available"
        assert stored_user.free_preview_analysis_id is None
        assert stored_user.free_preview_used_at is None
        for analysis_id in analysis_ids:
            stored_analysis = await session.get(Analysis, analysis_id)
            private = await session.get(AnalysisPrivateContent, analysis_id)
            assert stored_analysis is not None and stored_analysis.status == "deleted"
            assert stored_analysis.normalized_conversation_json is None
            assert stored_analysis.participants_json is None
            assert stored_analysis.user_participant_label is None
            assert stored_analysis.user_goal is None and stored_analysis.relationship_stage is None
            assert stored_analysis.result_json is None and stored_analysis.completed_at is None
            assert stored_analysis.message_count == 0 and stored_analysis.character_count == 0
            assert stored_analysis.report_access == "none"
            assert stored_analysis.feedback_score is None
            assert stored_analysis.feedback_submitted_at is None
            assert private is not None
            assert private.source_ciphertext is None and private.result_ciphertext is None
        stored_customer = await session.get(BillingCustomer, references[0])
        stored_subscription = await session.get(Subscription, references[1])
        stored_order = await session.get(PaymentOrder, references[2])
        stored_event = await session.get(ProviderWebhookEvent, references[3])
        stored_unrelated = await session.get(User, references[4])
        assert stored_customer is not None and stored_customer.provider_customer_id is None
        assert stored_subscription is not None and stored_subscription.status == "canceled"
        assert stored_subscription.encrypted_payment_method is None
        assert stored_order is not None and stored_order.checkout_url is None
        assert stored_order.encrypted_receipt_contact is None
        assert stored_event is not None and stored_event.status == "manual_review"
        assert stored_unrelated is not None and stored_unrelated.privacy_status == "active"
        stored_unrelated_analysis = await session.get(Analysis, unrelated_snapshot[4])
        assert (
            stored_unrelated.id,
            stored_unrelated.telegram_user_id,
            stored_unrelated.first_name,
            stored_unrelated.privacy_status,
            stored_unrelated_analysis.id if stored_unrelated_analysis else None,
            stored_unrelated_analysis.status if stored_unrelated_analysis else None,
            stored_unrelated_analysis.user_goal if stored_unrelated_analysis else None,
        ) == unrelated_snapshot
        for job_id in job_ids:
            stored_job = await session.get(BillingJob, job_id)
            assert stored_job is not None and stored_job.status == "manual_review"
        ledger = await session.get(CreditTransaction, transaction_snapshot[0])
        assert ledger is not None
        assert (
            ledger.id,
            ledger.user_id,
            ledger.type,
            ledger.amount,
            ledger.idempotency_key,
            ledger.payment_order_id,
            ledger.external_payment_provider,
            ledger.external_payment_id,
        ) == transaction_snapshot
        assert (
            await session.scalar(select(func.count()).select_from(CreditTransaction))
            == ledger_count
        )
    async with payment_db() as session:
        assert (
            await DataDeletionService(session, NoOpAnalyticsClient()).delete_account(user_id)
            is DataDeletionOutcome.ALREADY_DELETED
        )
