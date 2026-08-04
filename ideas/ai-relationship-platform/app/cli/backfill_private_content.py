"""Restartable legacy plaintext-to-ciphertext backfill."""

import argparse
import asyncio
import logging
from datetime import UTC, datetime, timedelta

from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import get_settings
from app.db.models import Analysis, AnalysisPrivateContent
from app.db.session import create_engine, create_session_factory
from app.services.sensitive_content import (
    AESGCMSensitiveContentCipher,
    ContentPurpose,
    decode_configured_key,
)

logger = logging.getLogger(__name__)


async def backfill_batch(
    session: AsyncSession,
    cipher: AESGCMSensitiveContentCipher,
    *,
    batch_size: int = 100,
    dry_run: bool = False,
    retention_days: int = 30,
) -> tuple[int, int]:
    # Kept as a command-level function so operations can test and orchestrate bounded batches.
    rows = list(
        (
            await session.scalars(
                select(Analysis)
                .where(
                    or_(
                        Analysis.normalized_conversation_json.is_not(None),
                        Analysis.participants_json.is_not(None),
                        Analysis.user_participant_label.is_not(None),
                        Analysis.user_goal.is_not(None),
                        Analysis.relationship_stage.is_not(None),
                        Analysis.result_json.is_not(None),
                    )
                )
                .order_by(Analysis.id)
                .limit(batch_size)
                .with_for_update(skip_locked=True)
            )
        ).all()
    )
    conflicts = 0
    for analysis in rows:
        private = await session.get(AnalysisPrivateContent, analysis.id)
        has_source = any(
            (
                analysis.normalized_conversation_json,
                analysis.participants_json,
                analysis.user_participant_label,
                analysis.user_goal,
                analysis.relationship_stage,
            )
        )
        if private and (
            (has_source and private.source_ciphertext)
            or (analysis.result_json is not None and private.result_ciphertext)
        ):
            conflicts += 1
            continue
        if dry_run:
            continue
        if private is None:
            private = AnalysisPrivateContent(analysis_id=analysis.id)
            session.add(private)
        if has_source:
            private.source_ciphertext = cipher.encrypt_json(
                ContentPurpose.ANALYSIS_SOURCE,
                {
                    "messages": analysis.normalized_conversation_json or [],
                    "participants": analysis.participants_json or {},
                    "user_participant_label": analysis.user_participant_label,
                    "user_goal": analysis.user_goal,
                    "relationship_stage": analysis.relationship_stage,
                },
            )
            private.source_format_version = 1
            private.source_delete_after = analysis.created_at + timedelta(days=retention_days)
        if analysis.result_json is not None:
            private.result_ciphertext = cipher.encrypt_json(
                ContentPurpose.ANALYSIS_RESULT, analysis.result_json
            )
            private.result_format_version = 1
        analysis.normalized_conversation_json = analysis.participants_json = None
        analysis.user_participant_label = analysis.user_goal = analysis.relationship_stage = None
        analysis.result_json = None
    if dry_run:
        await session.rollback()
    else:
        await session.commit()
    return len(rows), conflicts


async def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--batch-size", type=int, default=100)
    args = parser.parse_args()
    settings = get_settings()
    engine = create_engine(str(settings.database_url))
    sessions = create_session_factory(engine)
    cipher = AESGCMSensitiveContentCipher(
        decode_configured_key(settings.content_encryption_key.get_secret_value())
    )
    total = conflicts = 0
    async with sessions() as session:
        while True:
            count, found = await backfill_batch(
                session,
                cipher,
                batch_size=args.batch_size,
                dry_run=args.dry_run,
                retention_days=settings.raw_content_retention_days,
            )
            total += count
            conflicts += found
            if args.dry_run or count < args.batch_size or found == count:
                break
    logger.info(
        "private_content_backfill examined=%s conflicts=%s at=%s",
        total,
        conflicts,
        datetime.now(UTC).isoformat(),
    )
    await engine.dispose()
    return 1 if conflicts else 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
