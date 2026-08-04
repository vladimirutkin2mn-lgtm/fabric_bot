"""Deterministic payment completion and privacy deletion lock-overlap tests."""

# The closures below are created, awaited and discarded inside each iteration.
# ruff: noqa: B023

import asyncio
from collections.abc import Awaitable, Callable
from uuid import UUID

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
from app.providers.payments.gateway import AuthoritativePayment
from app.services.data_deletion import DataDeletionOutcome, DataDeletionService
from app.services.payment_completion_service import PaymentCompletionService
from tests.payment_postgres_helpers import create_claimed_job, create_order, paid

pytestmark = pytest.mark.postgres


async def _delete_account(
    sessions: async_sessionmaker[AsyncSession],
    user_id: UUID,
    *,
    after_user_lock: Callable[[], Awaitable[None]] | None = None,
) -> DataDeletionOutcome:
    async with sessions() as session:
        return await DataDeletionService(
            session,
            NoOpAnalyticsClient(),
            _after_user_lock_for_test=after_user_lock,
        ).delete_account(user_id)


async def _financial_counts(
    sessions: async_sessionmaker[AsyncSession], order_id: UUID
) -> tuple[int, int]:
    async with sessions() as session:
        purchases = await session.scalar(
            select(func.count())
            .select_from(CreditTransaction)
            .where(
                CreditTransaction.payment_order_id == order_id,
                CreditTransaction.type == "purchase",
            )
        )
        outbox = await session.scalar(
            select(func.count())
            .select_from(BillingOutboxEvent)
            .where(BillingOutboxEvent.idempotency_key == f"purchase_completed:{order_id}")
        )
    return int(purchases or 0), int(outbox or 0)


@pytest.mark.parametrize("source", ["webhook", "reconciliation"])
@pytest.mark.parametrize("first", ["deletion", "payment"])
async def test_claimed_payment_and_deletion_overlap_25_times(
    payment_db: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
    source: str,
    first: str,
) -> None:
    """Both claimed completion paths overlap the account-deletion transaction."""
    for iteration in range(25):
        checkout = f"overlap-{source}-{first}-{iteration}"
        payment_id = f"payment-{source}-{first}-{iteration}"
        user_id, order_id = await create_order(payment_db, checkout_id=checkout)
        job_id, claim_id, event_id = await create_claimed_job(
            payment_db,
            order_id,
            webhook=source == "webhook",
            checkout_id=checkout,
        )
        service = PaymentCompletionService(payment_db)
        authoritative = paid(order_id, checkout, payment_id)

        if first == "deletion":
            deletion_locked = asyncio.Event()
            payment_started = asyncio.Event()
            release_deletion = asyncio.Event()

            async def hold_deletion_lock() -> None:
                deletion_locked.set()
                await asyncio.wait_for(release_deletion.wait(), timeout=5)

            async def complete() -> str:
                payment_started.set()
                return await service.complete_claimed(
                    job_id,
                    claim_id,
                    order_id,
                    authoritative,
                )

            deletion_task = asyncio.create_task(
                _delete_account(
                    payment_db,
                    user_id,
                    after_user_lock=hold_deletion_lock,
                )
            )
            await asyncio.wait_for(deletion_locked.wait(), timeout=5)
            completion_task = asyncio.create_task(complete())
            await asyncio.wait_for(payment_started.wait(), timeout=5)
            await asyncio.sleep(0)
            assert not completion_task.done()
            release_deletion.set()
            completion, deletion = await asyncio.wait_for(
                asyncio.gather(completion_task, deletion_task), timeout=10
            )
            assert deletion is DataDeletionOutcome.DELETED
            assert completion in {"claim_lost", "user_deleted", "already_cancelled"}
            expected_count = 0
        else:
            payment_locked = asyncio.Event()
            deletion_started = asyncio.Event()
            release_payment = asyncio.Event()
            original_apply = service._apply_locked

            async def hold_payment_locks(
                session: AsyncSession,
                order: PaymentOrder,
                payment: AuthoritativePayment,
            ) -> str:
                payment_locked.set()
                await asyncio.wait_for(release_payment.wait(), timeout=5)
                return await original_apply(session, order, payment)

            monkeypatch.setattr(service, "_apply_locked", hold_payment_locks)

            async def delete() -> DataDeletionOutcome:
                deletion_started.set()
                return await _delete_account(payment_db, user_id)

            completion_task = asyncio.create_task(
                service.complete_claimed(job_id, claim_id, order_id, authoritative)
            )
            await asyncio.wait_for(payment_locked.wait(), timeout=5)
            deletion_task = asyncio.create_task(delete())
            await asyncio.wait_for(deletion_started.wait(), timeout=5)
            await asyncio.sleep(0)
            assert not deletion_task.done()
            release_payment.set()
            completion, deletion = await asyncio.wait_for(
                asyncio.gather(completion_task, deletion_task), timeout=10
            )
            assert completion == "completed"
            assert deletion is DataDeletionOutcome.DELETED
            expected_count = 1

        purchases, outbox = await _financial_counts(payment_db, order_id)
        assert purchases == expected_count and outbox == expected_count
        async with payment_db() as session:
            user = await session.get(User, user_id)
            order = await session.get(PaymentOrder, order_id)
            job = await session.get(BillingJob, job_id)
            event = (
                await session.get(ProviderWebhookEvent, event_id) if event_id is not None else None
            )
        assert user is not None and user.privacy_status == "deleted"
        assert order is not None
        assert job is not None
        if first == "deletion":
            assert order.status == "cancelled" and order.failure_code == "user_deleted"
            assert job.status == "manual_review" and job.last_error_code == "user_deleted"
            if event is not None:
                assert event.status == "manual_review"
                assert event.last_error_code == "user_deleted"
        else:
            assert order.status == "completed" and order.provider_payment_id == payment_id
            assert job.status == "completed" and job.claim_id is None
            if event is not None:
                assert event.status == "completed"


async def _create_external_identity_conflict(
    sessions: async_sessionmaker[AsyncSession], payment_id: str
) -> UUID:
    existing_user_id, existing_order_id = await create_order(
        sessions,
        checkout_id=f"existing-{payment_id}",
    )
    async with sessions.begin() as session:
        session.add(
            CreditTransaction(
                user_id=existing_user_id,
                type="purchase",
                amount=1,
                idempotency_key=f"preexisting:{payment_id}",
                payment_order_id=existing_order_id,
                product_code="analysis_single",
                external_payment_provider="stripe",
                external_payment_id=payment_id,
            )
        )
    return existing_order_id


@pytest.mark.parametrize("first", ["deletion", "recovery"])
async def test_identity_conflict_recovery_and_deletion_overlap_25_times(
    payment_db: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
    first: str,
) -> None:
    """A real ledger uniqueness conflict overlaps deletion in both lock schedules."""
    for iteration in range(25):
        checkout = f"identity-overlap-{first}-{iteration}"
        payment_id = f"identity-payment-{first}-{iteration}"
        existing_order_id = await _create_external_identity_conflict(payment_db, payment_id)
        user_id, order_id = await create_order(payment_db, checkout_id=checkout)
        job_id, claim_id, event_id = await create_claimed_job(
            payment_db,
            order_id,
            webhook=True,
            checkout_id=checkout,
        )
        assert event_id is not None
        service = PaymentCompletionService(payment_db)
        authoritative = paid(order_id, checkout, payment_id)

        if first == "deletion":
            recovery_called = asyncio.Event()
            deletion_locked = asyncio.Event()
            allow_recovery = asyncio.Event()
            release_deletion = asyncio.Event()
            original_recovery = service._resolve_claimed_identity_conflict

            async def pause_before_recovery(
                target_job_id: UUID,
                target_claim_id: UUID,
                target_order_id: UUID,
                target_payment_id: str,
            ) -> str:
                recovery_called.set()
                await asyncio.wait_for(allow_recovery.wait(), timeout=5)
                return await original_recovery(
                    target_job_id,
                    target_claim_id,
                    target_order_id,
                    target_payment_id,
                )

            async def hold_deletion_lock() -> None:
                deletion_locked.set()
                await asyncio.wait_for(release_deletion.wait(), timeout=5)

            monkeypatch.setattr(
                service,
                "_resolve_claimed_identity_conflict",
                pause_before_recovery,
            )
            completion_task = asyncio.create_task(
                service.complete_claimed(job_id, claim_id, order_id, authoritative)
            )
            await asyncio.wait_for(recovery_called.wait(), timeout=5)
            deletion_task = asyncio.create_task(
                _delete_account(
                    payment_db,
                    user_id,
                    after_user_lock=hold_deletion_lock,
                )
            )
            await asyncio.wait_for(deletion_locked.wait(), timeout=5)
            allow_recovery.set()
            await asyncio.sleep(0)
            assert not completion_task.done()
            release_deletion.set()
            completion, deletion = await asyncio.wait_for(
                asyncio.gather(completion_task, deletion_task), timeout=10
            )
            assert completion == "claim_lost"
            assert deletion is DataDeletionOutcome.DELETED
        else:
            recovery_locked = asyncio.Event()
            deletion_started = asyncio.Event()
            release_recovery = asyncio.Event()
            original_lock_order = service._lock_order
            lock_calls = 0

            async def pause_second_order_lock(
                session: AsyncSession,
                target_order_id: UUID,
            ) -> PaymentOrder | None:
                nonlocal lock_calls
                order = await original_lock_order(session, target_order_id)
                lock_calls += 1
                if lock_calls == 2:
                    recovery_locked.set()
                    await asyncio.wait_for(release_recovery.wait(), timeout=5)
                return order

            monkeypatch.setattr(service, "_lock_order", pause_second_order_lock)

            async def delete() -> DataDeletionOutcome:
                deletion_started.set()
                return await _delete_account(payment_db, user_id)

            completion_task = asyncio.create_task(
                service.complete_claimed(job_id, claim_id, order_id, authoritative)
            )
            await asyncio.wait_for(recovery_locked.wait(), timeout=5)
            deletion_task = asyncio.create_task(delete())
            await asyncio.wait_for(deletion_started.wait(), timeout=5)
            await asyncio.sleep(0)
            assert not deletion_task.done()
            release_recovery.set()
            completion, deletion = await asyncio.wait_for(
                asyncio.gather(completion_task, deletion_task), timeout=10
            )
            assert completion == "manual_review"
            assert deletion is DataDeletionOutcome.DELETED

        async with payment_db() as session:
            user = await session.get(User, user_id)
            order = await session.get(PaymentOrder, order_id)
            job = await session.get(BillingJob, job_id)
            event = await session.get(ProviderWebhookEvent, event_id)
            conflict_count = await session.scalar(
                select(func.count())
                .select_from(CreditTransaction)
                .where(
                    CreditTransaction.external_payment_provider == "stripe",
                    CreditTransaction.external_payment_id == payment_id,
                )
            )
            target_purchases = await session.scalar(
                select(func.count())
                .select_from(CreditTransaction)
                .where(CreditTransaction.payment_order_id == order_id)
            )
            target_outbox = await session.scalar(
                select(func.count())
                .select_from(BillingOutboxEvent)
                .where(BillingOutboxEvent.aggregate_id == str(order_id))
            )
            existing = await session.scalar(
                select(CreditTransaction).where(
                    CreditTransaction.payment_order_id == existing_order_id
                )
            )
        assert user is not None and user.privacy_status == "deleted"
        assert order is not None and order.status in {"cancelled", "manual_review"}
        assert job is not None and job.status == "manual_review"
        assert event is not None and event.status == "manual_review"
        assert conflict_count == 1
        assert target_purchases == 0 and target_outbox == 0
        assert existing is not None
        assert existing.external_payment_id == payment_id
        assert existing.external_payment_provider == "stripe"
