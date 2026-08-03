"""PostgreSQL regressions for monotonic preview and full report access."""

import asyncio
import os
from collections.abc import AsyncIterator
from datetime import UTC, datetime
from uuid import UUID, uuid4

import pytest
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.db.base import Base
from app.db.models import Analysis, CreditTransaction, User
from app.providers.analytics import NoOpAnalyticsClient
from app.services.analysis_service import AnalysisServiceResult
from app.services.credits_service import CreditsService, SpendOutcome
from app.services.monetized_analysis import AccessOutcome, MonetizedAnalysisService
from app.services.preview_entitlement import PreviewEntitlementService, PreviewOutcome
from tests.test_report_service import payload

pytestmark = pytest.mark.postgres


@pytest.fixture
async def monotonic_db() -> AsyncIterator[async_sessionmaker[AsyncSession]]:
    url = os.getenv("TEST_DATABASE_URL")
    if not url:
        pytest.skip("TEST_DATABASE_URL is required")
    engine = create_async_engine(url)
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.drop_all)
        await connection.run_sync(Base.metadata.create_all)
    yield async_sessionmaker(engine, expire_on_commit=False)
    await engine.dispose()


class NeverRun:
    async def analyze(self, analysis_id: UUID, owner_id: UUID) -> AnalysisServiceResult:
        raise AssertionError("analysis must not run")


async def _draft(
    sessions: async_sessionmaker[AsyncSession],
) -> tuple[UUID, UUID]:
    async with sessions.begin() as session:
        user = User(telegram_user_id=uuid4().int % 10**12, first_name="Fictional")
        session.add(user)
        await session.flush()
        analysis = Analysis(user_id=user.id, status="draft", intake_step="complete")
        session.add(analysis)
        await session.flush()
        return user.id, analysis.id


async def _complete(
    sessions: async_sessionmaker[AsyncSession], analysis_id: UUID
) -> None:
    async with sessions.begin() as session:
        analysis = await session.get(Analysis, analysis_id)
        assert analysis is not None
        analysis.status = "completed"
        analysis.result_json = payload()
        analysis.completed_at = datetime.now(UTC)


def _monetized(
    sessions: async_sessionmaker[AsyncSession],
    credits: CreditsService,
    previews: PreviewEntitlementService,
) -> MonetizedAnalysisService:
    return MonetizedAnalysisService(
        sessions,
        credits,
        previews,
        NeverRun(),
        1,
        NoOpAnalyticsClient(),
    )


async def test_preview_finalization_preserves_existing_full_access(
    monotonic_db: async_sessionmaker[AsyncSession],
) -> None:
    user_id, analysis_id = await _draft(monotonic_db)
    credits = CreditsService(monotonic_db)
    previews = PreviewEntitlementService(monotonic_db)
    monetized = _monetized(monotonic_db, credits, previews)

    assert await previews.reserve_preview(user_id, analysis_id) is PreviewOutcome.RESERVED
    await credits.grant(user_id, 1, "monotonic:deterministic:grant")
    spent = await credits.spend(user_id, analysis_id, 1)
    assert spent.outcome is SpendOutcome.SPENT and spent.transaction_id is not None
    await _complete(monotonic_db, analysis_id)
    assert (
        await monetized._set_access(analysis_id, user_id, "full", 1, spent.transaction_id)
        is AccessOutcome.UPDATED
    )

    assert await previews.finalize_preview(user_id, analysis_id) is PreviewOutcome.CONSUMED

    async with monotonic_db() as session:
        analysis = await session.get(Analysis, analysis_id)
        user = await session.get(User, user_id)
        refund_count = await session.scalar(
            select(func.count())
            .select_from(CreditTransaction)
            .where(CreditTransaction.reverses_transaction_id == spent.transaction_id)
        )
        assert analysis is not None and user is not None
        assert analysis.report_access == "full"
        assert analysis.cost_units == 1
        assert analysis.full_access_transaction_id == spent.transaction_id
        assert user.free_preview_status == "consumed"
        assert refund_count == 0


async def test_normal_preview_finalization_remains_idempotently_terminal(
    monotonic_db: async_sessionmaker[AsyncSession],
) -> None:
    user_id, analysis_id = await _draft(monotonic_db)
    previews = PreviewEntitlementService(monotonic_db)

    assert await previews.reserve_preview(user_id, analysis_id) is PreviewOutcome.RESERVED
    await _complete(monotonic_db, analysis_id)
    assert await previews.finalize_preview(user_id, analysis_id) is PreviewOutcome.CONSUMED
    assert await previews.finalize_preview(user_id, analysis_id) is PreviewOutcome.UNAVAILABLE

    async with monotonic_db() as session:
        analysis = await session.get(Analysis, analysis_id)
        user = await session.get(User, user_id)
        assert analysis is not None and user is not None
        assert analysis.report_access == "preview"
        assert analysis.cost_units == 0
        assert analysis.full_access_transaction_id is None
        assert user.free_preview_status == "consumed"


async def test_preview_and_full_race_always_finishes_with_full_access(
    monotonic_db: async_sessionmaker[AsyncSession],
) -> None:
    for iteration in range(25):
        user_id, analysis_id = await _draft(monotonic_db)
        credits = CreditsService(monotonic_db)
        previews = PreviewEntitlementService(monotonic_db)
        monetized = _monetized(monotonic_db, credits, previews)

        assert await previews.reserve_preview(user_id, analysis_id) is PreviewOutcome.RESERVED
        await credits.grant(user_id, 1, f"monotonic:race:{iteration}:grant")
        spent = await credits.spend(user_id, analysis_id, 1)
        assert spent.outcome is SpendOutcome.SPENT and spent.transaction_id is not None
        await _complete(monotonic_db, analysis_id)

        preview_outcome, access_outcome = await asyncio.gather(
            previews.finalize_preview(user_id, analysis_id),
            monetized._set_access(analysis_id, user_id, "full", 1, spent.transaction_id),
        )
        assert preview_outcome is PreviewOutcome.CONSUMED
        assert access_outcome is AccessOutcome.UPDATED

        async with monotonic_db() as session:
            analysis = await session.get(Analysis, analysis_id)
            user = await session.get(User, user_id)
            refund_count = await session.scalar(
                select(func.count())
                .select_from(CreditTransaction)
                .where(CreditTransaction.reverses_transaction_id == spent.transaction_id)
            )
            assert analysis is not None and user is not None
            assert analysis.report_access == "full"
            assert analysis.cost_units == 1
            assert analysis.full_access_transaction_id == spent.transaction_id
            assert user.free_preview_status == "consumed"
            assert refund_count == 0
