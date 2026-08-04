"""Interrupted analysis recovery and retry tests on real PostgreSQL."""

import asyncio
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.db.models import Analysis, CreditTransaction, User
from app.services.analysis_recovery import (
    AnalysisRetryOutcome,
    requeue_stale_processing,
    retry_failed_analysis,
)

pytestmark = pytest.mark.postgres


async def test_only_stale_processing_is_requeued(
    payment_db: async_sessionmaker[AsyncSession],
) -> None:
    now = datetime.now(UTC)
    async with payment_db.begin() as session:
        user = User(telegram_user_id=880001, first_name="Recovery")
        session.add(user)
        await session.flush()
        stale = Analysis(
            user_id=user.id,
            status="processing",
            intake_step="complete",
            processing_started_at=now - timedelta(minutes=20),
        )
        fresh = Analysis(
            user_id=user.id,
            status="processing",
            intake_step="complete",
            processing_started_at=now - timedelta(seconds=30),
        )
        session.add_all([stale, fresh])
        await session.flush()
        stale_id, fresh_id = stale.id, fresh.id

    async with payment_db() as session:
        result = await requeue_stale_processing(
            session,
            stale_after_seconds=900,
            batch_size=10,
            now=now,
        )
        assert result.examined == 1 and result.requeued == 1
        assert result.financially_closed == 0

    async with payment_db() as session:
        stored_stale = await session.get(Analysis, stale_id)
        stored_fresh = await session.get(Analysis, fresh_id)
        assert stored_stale is not None and stored_stale.status == "draft"
        assert stored_stale.processing_started_at is None
        assert stored_fresh is not None and stored_fresh.status == "processing"


async def test_refunded_stale_processing_is_financially_closed(
    payment_db: async_sessionmaker[AsyncSession],
) -> None:
    now = datetime.now(UTC)
    async with payment_db.begin() as session:
        user = User(telegram_user_id=880003, first_name="Recovery")
        session.add(user)
        await session.flush()
        analysis = Analysis(
            user_id=user.id,
            status="processing",
            intake_step="complete",
            processing_started_at=now - timedelta(minutes=20),
        )
        session.add(analysis)
        await session.flush()
        spend = CreditTransaction(
            user_id=user.id,
            type="spend",
            amount=-1,
            idempotency_key=f"analysis_full_access:{analysis.id}",
            analysis_id=analysis.id,
        )
        session.add(spend)
        await session.flush()
        session.add(
            CreditTransaction(
                user_id=user.id,
                type="refund",
                amount=1,
                idempotency_key=f"refund:{spend.id}",
                analysis_id=analysis.id,
                reverses_transaction_id=spend.id,
            )
        )
        analysis_id = analysis.id

    async with payment_db() as session:
        result = await requeue_stale_processing(
            session,
            stale_after_seconds=900,
            now=now,
        )
        assert result.examined == 1
        assert result.requeued == 0
        assert result.financially_closed == 1

    async with payment_db() as session:
        stored = await session.get(Analysis, analysis_id)
        assert stored is not None and stored.status == "failed"
        assert stored.failure_code == "worker_interrupted_refunded"
        assert stored.processing_started_at is None


async def test_two_recovery_workers_partition_stale_claims(
    payment_db: async_sessionmaker[AsyncSession],
) -> None:
    now = datetime.now(UTC)
    async with payment_db.begin() as session:
        user = User(telegram_user_id=880002, first_name="Recovery")
        session.add(user)
        await session.flush()
        for index in range(20):
            session.add(
                Analysis(
                    user_id=user.id,
                    status="processing",
                    intake_step="complete",
                    processing_started_at=now - timedelta(minutes=20, seconds=index),
                )
            )

    first_selected = asyncio.Event()
    second_selected = asyncio.Event()
    batches: list[tuple[object, ...]] = []

    async def first_hook(ids: tuple[object, ...]) -> None:
        batches.append(ids)
        first_selected.set()
        await asyncio.wait_for(second_selected.wait(), timeout=5)

    async def second_hook(ids: tuple[object, ...]) -> None:
        batches.append(ids)
        second_selected.set()

    async def first_worker() -> int:
        async with payment_db() as session:
            return (
                await requeue_stale_processing(
                    session,
                    stale_after_seconds=900,
                    batch_size=10,
                    now=now,
                    after_lock=first_hook,
                )
            ).requeued

    async def second_worker() -> int:
        await asyncio.wait_for(first_selected.wait(), timeout=5)
        async with payment_db() as session:
            return (
                await requeue_stale_processing(
                    session,
                    stale_after_seconds=900,
                    batch_size=10,
                    now=now,
                    after_lock=second_hook,
                )
            ).requeued

    counts = await asyncio.gather(first_worker(), second_worker())
    assert len(batches) == 2 and all(batches)
    assert set(batches[0]).isdisjoint(batches[1])
    assert sum(counts) == 20


@pytest.mark.parametrize(
    ("failure_code", "expected"),
    [
        ("llm_timeout", AnalysisRetryOutcome.REQUEUED),
        ("invalid_model_output", AnalysisRetryOutcome.NOT_RETRYABLE),
    ],
)
async def test_retry_failed_analysis_allows_only_transient_failures(
    payment_db: async_sessionmaker[AsyncSession],
    failure_code: str,
    expected: AnalysisRetryOutcome,
) -> None:
    async with payment_db.begin() as session:
        user = User(
            telegram_user_id=880100 if expected is AnalysisRetryOutcome.REQUEUED else 880101,
            first_name="Retry",
        )
        session.add(user)
        await session.flush()
        analysis = Analysis(
            user_id=user.id,
            status="failed",
            intake_step="complete",
            failure_code=failure_code,
            llm_provider="openai",
            model_name="model",
            prompt_version="analysis_v1",
            llm_attempt_count=2,
        )
        session.add(analysis)
        await session.flush()
        analysis_id, user_id = analysis.id, user.id

    async with payment_db() as session:
        assert await retry_failed_analysis(session, analysis_id, user_id) is expected

    async with payment_db() as session:
        stored = await session.get(Analysis, analysis_id)
        assert stored is not None
        if expected is AnalysisRetryOutcome.REQUEUED:
            assert stored.status == "draft"
            assert stored.failure_code is None
            assert stored.llm_attempt_count == 0
            assert stored.llm_provider is None
        else:
            assert stored.status == "failed"
            assert stored.failure_code == failure_code


async def test_explicit_retry_rejects_refunded_paid_analysis(
    payment_db: async_sessionmaker[AsyncSession],
) -> None:
    async with payment_db.begin() as session:
        user = User(telegram_user_id=880102, first_name="Retry")
        session.add(user)
        await session.flush()
        analysis = Analysis(
            user_id=user.id,
            status="failed",
            intake_step="complete",
            failure_code="llm_timeout",
        )
        session.add(analysis)
        await session.flush()
        spend = CreditTransaction(
            user_id=user.id,
            type="spend",
            amount=-1,
            idempotency_key=f"analysis_full_access:{analysis.id}",
            analysis_id=analysis.id,
        )
        session.add(spend)
        await session.flush()
        session.add(
            CreditTransaction(
                user_id=user.id,
                type="refund",
                amount=1,
                idempotency_key=f"refund:{spend.id}",
                analysis_id=analysis.id,
                reverses_transaction_id=spend.id,
            )
        )
        analysis_id, user_id = analysis.id, user.id

    async with payment_db() as session:
        outcome = await retry_failed_analysis(session, analysis_id, user_id)
        assert outcome is AnalysisRetryOutcome.FINANCIALLY_CLOSED

    async with payment_db() as session:
        stored = await session.get(Analysis, analysis_id)
        assert stored is not None and stored.status == "failed"
        assert stored.failure_code == "llm_timeout"
