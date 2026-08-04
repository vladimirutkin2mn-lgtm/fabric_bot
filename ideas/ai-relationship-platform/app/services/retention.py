"""Bounded, lock-safe source retention cleanup."""

from dataclasses import dataclass
from datetime import UTC, datetime

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
) -> RetentionResult:
    cutoff = now or datetime.now(UTC)
    rows = list(
        (
            await session.scalars(
                select(AnalysisPrivateContent)
                .where(
                    AnalysisPrivateContent.source_ciphertext.is_not(None),
                    AnalysisPrivateContent.source_delete_after <= cutoff,
                )
                .order_by(
                    AnalysisPrivateContent.source_delete_after, AnalysisPrivateContent.analysis_id
                )
                .limit(batch_size)
                .with_for_update(skip_locked=True)
            )
        ).all()
    )
    if dry_run:
        await session.rollback()
        return RetentionResult(len(rows), 0)
    for private in rows:
        analysis = await session.get(Analysis, private.analysis_id, with_for_update=True)
        private.source_ciphertext = None
        private.source_deleted_at = cutoff
        if analysis:
            analysis.normalized_conversation_json = analysis.participants_json = None
            analysis.user_participant_label = analysis.user_goal = analysis.relationship_stage = (
                None
            )
            if analysis.status in {"draft", "queued", "processing"}:
                analysis.status, analysis.deleted_at = "deleted", cutoff
                analysis.report_access = "none"
    await session.commit()
    return RetentionResult(len(rows), len(rows))
