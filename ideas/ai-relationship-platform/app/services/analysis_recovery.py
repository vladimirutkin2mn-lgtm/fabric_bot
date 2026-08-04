"""Bounded recovery and explicit retry for interrupted analysis jobs."""

from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from enum import StrEnum
from uuid import UUID

from sqlalchemy import select, update
from sqlalchemy.engine import CursorResult
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import Analysis

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


class AnalysisRetryOutcome(StrEnum):
    REQUEUED = "requeued"
    NOT_RETRYABLE = "not_retryable"
    NOT_FAILED = "not_failed"
    NOT_FOUND = "not_found"


async def requeue_stale_processing(
    session: AsyncSession,
    *,
    stale_after_seconds: int,
    batch_size: int = 100,
    now: datetime | None = None,
    after_lock: AfterLockHook | None = None,
) -> AnalysisRecoveryResult:
    """Return expired processing claims to a retryable draft state exactly once."""
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

    changed = await session.execute(
        update(Analysis)
        .where(
            Analysis.id.in_(ids),
            Analysis.status == "processing",
            Analysis.processing_started_at <= cutoff,
        )
        .values(
            status="draft",
            processing_started_at=None,
            failure_code=None,
            completed_at=None,
        )
    )
    await session.commit()
    rowcount = changed.rowcount if isinstance(changed, CursorResult) else 0
    return AnalysisRecoveryResult(examined=len(ids), requeued=max(rowcount, 0))


async def retry_failed_analysis(
    session: AsyncSession,
    analysis_id: UUID,
    user_id: UUID,
) -> AnalysisRetryOutcome:
    """Requeue only known transient failures without changing financial state."""
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
