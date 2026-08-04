"""Bounded recovery and explicit retry for interrupted analysis jobs."""

from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from enum import StrEnum
from typing import cast
from uuid import UUID

from sqlalchemy import select, update
from sqlalchemy.engine import CursorResult
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import aliased

from app.db.models import Analysis, CreditTransaction

AfterLockHook = Callable[[tuple[UUID, ...]], Awaitable[None]]

_RETRYABLE_FAILURE_CODES = frozenset(
    {
        "llm_timeout",
        "llm_rate_limited",
        "llm_transient_error",
        "unexpected_provider_error",
        "unexpected_pipeline_error",
        "worker_interrupted",
    }
)


@dataclass(frozen=True)
class AnalysisRecoveryResult:
    examined: int
    requeued: int
    financially_closed: int = 0


class AnalysisRetryOutcome(StrEnum):
    REQUEUED = "requeued"
    FINANCIALLY_CLOSED = "financially_closed"
    NOT_RETRYABLE = "not_retryable"
    NOT_FAILED = "not_failed"
    NOT_FOUND = "not_found"


async def _refunded_analysis_ids(
    session: AsyncSession, analysis_ids: tuple[UUID, ...]
) -> frozenset[UUID]:
    if not analysis_ids:
        return frozenset()
    spend = aliased(CreditTransaction)
    refund = aliased(CreditTransaction)
    rows = await session.scalars(
        select(spend.analysis_id)
        .join(refund, refund.reverses_transaction_id == spend.id)
        .where(
            spend.analysis_id.in_(analysis_ids),
            spend.type == "spend",
            refund.type == "refund",
        )
    )
    return frozenset(value for value in rows if value is not None)


async def requeue_stale_processing(
    session: AsyncSession,
    *,
    stale_after_seconds: int,
    batch_size: int = 100,
    now: datetime | None = None,
    after_lock: AfterLockHook | None = None,
) -> AnalysisRecoveryResult:
    """Recover expired processing claims without reopening refunded paid work."""
    if stale_after_seconds <= 0 or batch_size <= 0:
        raise ValueError("recovery bounds must be positive")
    cutoff = (now or datetime.now(UTC)) - timedelta(seconds=stale_after_seconds)
    statement = (
        select(Analysis.id)
        .where(
            Analysis.status == "processing",
            Analysis.processing_started_at.is_not(None),
            Analysis.processing_started_at <= cutoff,
        )
        .order_by(Analysis.processing_started_at, Analysis.id)
        .with_for_update(skip_locked=True)
        .limit(batch_size)
    )
    ids = tuple((await session.scalars(statement)).all())
    if after_lock is not None:
        await after_lock(ids)
    if not ids:
        await session.commit()
        return AnalysisRecoveryResult(examined=0, requeued=0)

    refunded = await _refunded_analysis_ids(session, ids)
    retryable = tuple(analysis_id for analysis_id in ids if analysis_id not in refunded)
    requeued = 0
    if retryable:
        changed = cast(
            CursorResult[object],
            await session.execute(
                update(Analysis)
                .where(
                    Analysis.id.in_(retryable),
                    Analysis.status == "processing",
                    Analysis.processing_started_at <= cutoff,
                )
                .values(
                    status="draft",
                    processing_started_at=None,
                    failure_code=None,
                    completed_at=None,
                )
            ),
        )
        requeued = changed.rowcount
    closed = 0
    if refunded:
        changed = cast(
            CursorResult[object],
            await session.execute(
                update(Analysis)
                .where(
                    Analysis.id.in_(refunded),
                    Analysis.status == "processing",
                    Analysis.processing_started_at <= cutoff,
                )
                .values(
                    status="failed",
                    processing_started_at=None,
                    failure_code="worker_interrupted_refunded",
                    completed_at=None,
                )
            ),
        )
        closed = changed.rowcount
    await session.commit()
    return AnalysisRecoveryResult(
        examined=len(ids),
        requeued=requeued,
        financially_closed=closed,
    )


async def retry_failed_analysis(
    session: AsyncSession,
    analysis_id: UUID,
    user_id: UUID,
) -> AnalysisRetryOutcome:
    """Requeue known transient failures unless the associated spend was refunded."""
    analysis = await session.scalar(
        select(Analysis)
        .where(Analysis.id == analysis_id, Analysis.user_id == user_id)
        .with_for_update()
    )
    if analysis is None:
        await session.rollback()
        return AnalysisRetryOutcome.NOT_FOUND
    if analysis.status != "failed":
        await session.rollback()
        return AnalysisRetryOutcome.NOT_FAILED
    if analysis.failure_code not in _RETRYABLE_FAILURE_CODES:
        await session.rollback()
        return AnalysisRetryOutcome.NOT_RETRYABLE
    if analysis.id in await _refunded_analysis_ids(session, (analysis.id,)):
        await session.rollback()
        return AnalysisRetryOutcome.FINANCIALLY_CLOSED

    analysis.status = "draft"
    analysis.processing_started_at = None
    analysis.completed_at = None
    analysis.failure_code = None
    analysis.llm_provider = None
    analysis.model_name = None
    analysis.prompt_version = None
    analysis.llm_attempt_count = 0
    analysis.input_tokens = None
    analysis.output_tokens = None
    analysis.latency_ms = None
    analysis.provider_request_id = None
    analysis.result_json = None
    await session.commit()
    return AnalysisRetryOutcome.REQUEUED
