"""PostgreSQL coverage for explicit encrypted-content ownership joins."""

import os
from collections.abc import AsyncIterator

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.db.base import Base
from app.db.models import Analysis, User
from app.repositories.private_content import AnalysisSource, EncryptedAnalysisContentRepository
from app.services.sensitive_content import AESGCMSensitiveContentCipher

pytestmark = pytest.mark.postgres


@pytest.fixture
async def private_sessions() -> AsyncIterator[async_sessionmaker[AsyncSession]]:
    url = os.getenv("TEST_DATABASE_URL")
    if not url:
        pytest.skip("TEST_DATABASE_URL is required for PostgreSQL integration tests")
    engine = create_async_engine(url)
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.drop_all)
        await connection.run_sync(Base.metadata.create_all)
    yield async_sessionmaker(engine, expire_on_commit=False)
    await engine.dispose()


async def test_explicit_source_and_result_joins_enforce_active_ownership(
    private_sessions: async_sessionmaker[AsyncSession],
) -> None:
    cipher = AESGCMSensitiveContentCipher("private-content-postgres-test-key-material")
    async with private_sessions.begin() as session:
        user = User(telegram_user_id=991001, first_name="Test")
        session.add(user)
        await session.flush()
        analysis = Analysis(
            user_id=user.id,
            status="completed",
            intake_step="complete",
            completed_at=user.created_at,
            report_access="full",
        )
        session.add(analysis)
        await session.flush()
        content = EncryptedAnalysisContentRepository(session, cipher, 30)
        await content.store_source(
            analysis.id, AnalysisSource([], {"A": "One", "B": "Two"}), replace=True
        )
        assert await content.store_result(analysis.id, {"summary": "private"})
    async with private_sessions.begin() as session:
        content = EncryptedAnalysisContentRepository(session, cipher, 30)
        assert await content.load_source(analysis.id, user.id) is not None
        assert await content.load_result(analysis.id, user.id) == {"summary": "private"}
        stored_analysis = await session.get(Analysis, analysis.id)
        assert stored_analysis is not None
        stored_analysis.status, stored_analysis.report_access = "deleted", "none"
        await session.flush()
        assert await content.load_source(analysis.id, user.id) is None
        assert await content.load_result(analysis.id, user.id) is None
        stored_analysis.status, stored_analysis.report_access = "completed", "full"
        stored_user = await session.get(User, user.id)
        assert stored_user is not None
        stored_user.privacy_status = "deleted"
        stored_user.telegram_user_id = stored_user.first_name = None
        stored_user.deleted_at = stored_user.created_at
    async with private_sessions() as session:
        content = EncryptedAnalysisContentRepository(session, cipher, 30)
        assert await content.load_source(analysis.id, user.id) is None
        assert await content.load_result(analysis.id, user.id) is None
