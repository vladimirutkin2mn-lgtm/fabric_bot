"""Source-retention state transitions on isolated PostgreSQL."""

from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.db.models import Analysis, AnalysisPrivateContent, User
from app.services.retention import cleanup_expired_source

pytestmark = pytest.mark.postgres


@pytest.mark.parametrize("status", ["draft", "queued", "processing"])
async def test_expired_incomplete_source_becomes_deleted(
    payment_db: async_sessionmaker[AsyncSession], status: str
) -> None:
    async with payment_db.begin() as session:
        user = User(telegram_user_id=740001, first_name="Retention")
        session.add(user)
        await session.flush()
        analysis = Analysis(user_id=user.id, status=status, intake_step="complete")
        session.add(analysis)
        await session.flush()
        session.add(
            AnalysisPrivateContent(
                analysis_id=analysis.id,
                source_ciphertext=b"expired",
                result_ciphertext=b"preserve-if-present",
                source_delete_after=datetime.now(UTC) - timedelta(seconds=1),
            )
        )
        analysis_id = analysis.id
    async with payment_db() as session:
        result = await cleanup_expired_source(session, batch_size=10)
        assert result.cleared == 1
    async with payment_db() as session:
        stored_analysis = await session.get(Analysis, analysis_id)
        private = await session.get(AnalysisPrivateContent, analysis_id)
        assert stored_analysis is not None and stored_analysis.status == "deleted"
        assert private is not None and private.source_ciphertext is None
        assert private.result_ciphertext == b"preserve-if-present"


async def test_retention_dry_run_and_repeat_are_noops(
    payment_db: async_sessionmaker[AsyncSession],
) -> None:
    async with payment_db.begin() as session:
        user = User(telegram_user_id=740002, first_name="Retention")
        session.add(user)
        await session.flush()
        analysis = Analysis(user_id=user.id, status="draft", intake_step="complete")
        session.add(analysis)
        await session.flush()
        session.add(
            AnalysisPrivateContent(
                analysis_id=analysis.id,
                source_ciphertext=b"expired",
                source_delete_after=datetime.now(UTC) - timedelta(seconds=1),
            )
        )
        analysis_id = analysis.id
    async with payment_db() as session:
        assert (await cleanup_expired_source(session, dry_run=True)).cleared == 0
    async with payment_db() as session:
        private = await session.get(AnalysisPrivateContent, analysis_id)
        assert private is not None and private.source_ciphertext == b"expired"
    async with payment_db() as session:
        assert (await cleanup_expired_source(session)).cleared == 1
    async with payment_db() as session:
        assert (await cleanup_expired_source(session)).cleared == 0
