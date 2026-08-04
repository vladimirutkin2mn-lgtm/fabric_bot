"""Bounded, lock-safe source retention cleanup."""

from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import Analysis, AnalysisPrivateContent


@dataclass(frozen=True)
class RetentionResult:
    examined: int
    cleared: int


async def cleanup_expired_source(
    session: AsyncSession,
    *,
    batch_size: int = 100,
    dry_run: bool = False,
    now: datetime | None = None,
    after_lock: Callable[[tuple[UUID, ...]], Awaitable[None]] | None = None,
) -> RetentionResult:
    cutoff = now or datetime.now(UTC)
    analyses = list(
        (
            await session.scalars(
                select(Analysis)
                .join(AnalysisPrivateContent)
                .where(
                    AnalysisPrivateContent.source_ciphertext.is_not(None),
                    AnalysisPrivateContent.source_delete_after <= cutoff,
                )
                .order_by(AnalysisPrivateContent.source_delete_after, Analysis.id)
                .limit(batch_size)
                .with_for_update(of=Analysis, skip_locked=True)
            )
        ).all()
    )
    if after_lock is not None:
        await after_lock(tuple(analysis.id for analysis in analyses))
    if dry_run:
        await session.rollback()
        return RetentionResult(len(analyses), 0)
    cleared = 0
    for analysis in analyses:
        private = await session.get(AnalysisPrivateContent, analysis.id, with_for_update=True)
        if (
            private is None
            or private.source_ciphertext is None
            or private.source_delete_after is None
            or private.source_delete_after > cutoff
        ):
            continue
        private.source_ciphertext = None
        private.source_deleted_at = cutoff
        analysis.normalized_conversation_json = analysis.participants_json = None
        analysis.user_participant_label = analysis.user_goal = analysis.relationship_stage = None
        if analysis.status in {"draft", "queued", "processing"}:
            analysis.status, analysis.deleted_at = "deleted", cutoff
            analysis.report_access = "none"
        cleared += 1
    await session.commit()
    return RetentionResult(len(analyses), cleared)
