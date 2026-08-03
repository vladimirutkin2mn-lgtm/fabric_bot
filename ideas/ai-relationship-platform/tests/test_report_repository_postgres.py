"""Milestone 4 history, feedback concurrency, ownership, and deletion on PostgreSQL."""

import asyncio
import os
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import (
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from app.db.base import Base
from app.db.models import Analysis, User
from app.repositories.analyses import DeletionOutcome, FeedbackOutcome, SqlAlchemyAnalysisRepository
from app.services.report_renderer import ReportRenderer
from app.services.report_service import ReportService
from tests.test_report_service import Analytics, payload

pytestmark = pytest.mark.postgres


@pytest.fixture
async def postgres_m4() -> AsyncIterator[async_sessionmaker[AsyncSession]]:
    url = os.getenv("TEST_DATABASE_URL")
    if not url:
        pytest.skip("TEST_DATABASE_URL is required for PostgreSQL integration tests")
    engine = create_async_engine(url)
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.drop_all)
        await connection.run_sync(Base.metadata.create_all)
    yield async_sessionmaker(engine, expire_on_commit=False)
    await engine.dispose()


async def create_row(
    sessions: async_sessionmaker[AsyncSession],
    status: str = "completed",
    user_id: UUID | None = None,
    completed_at: datetime | None = None,
) -> Analysis:
    async with sessions() as session:
        if user_id is None:
            user = User(telegram_user_id=uuid4().int % 10**12, first_name="Fictional")
            session.add(user)
            await session.flush()
            user_id = user.id
        row = Analysis(
            user_id=user_id,
            status=status,
            intake_step="complete",
            normalized_conversation_json=[
                {
                    "id": "m1",
                    "speaker": "A",
                    "timestamp": None,
                    "text": "PRIVATE",
                    "source_order": 1,
                }
            ],
            participants_json={"A": "One", "B": "Two"},
            user_participant_label="A",
            user_goal="Goal",
            relationship_stage="dating",
            message_count=1,
            character_count=7,
            result_json=payload() if status == "completed" else None,
            completed_at=(completed_at or datetime.now(UTC)) if status == "completed" else None,
            failure_code="safe" if status == "failed" else None,
        )
        session.add(row)
        await session.commit()
        return row


async def test_feedback_ten_way_has_one_winner_and_one_event(
    postgres_m4: async_sessionmaker[AsyncSession],
) -> None:
    row = await create_row(postgres_m4)
    analytics = Analytics()

    async def submit(score: int) -> FeedbackOutcome:
        async with postgres_m4() as session:
            return await ReportService(
                SqlAlchemyAnalysisRepository(session), ReportRenderer(), analytics
            ).feedback(row.id, row.user_id, score)

    outcomes = await asyncio.gather(*(submit((index % 5) + 1) for index in range(10)))
    assert outcomes.count(FeedbackOutcome.RECORDED) == 1
    assert outcomes.count(FeedbackOutcome.ALREADY_RECORDED) == 9
    assert analytics.events == ["analysis_feedback_submitted"]
    async with postgres_m4() as session:
        stored = await session.get(Analysis, row.id)
        assert stored and stored.feedback_score in range(1, 6) and stored.feedback_submitted_at


@pytest.mark.parametrize("score", [-1, 0, 6])
async def test_invalid_feedback_has_distinct_outcome(
    postgres_m4: async_sessionmaker[AsyncSession], score: int
) -> None:
    row = await create_row(postgres_m4)
    async with postgres_m4() as session:
        assert (
            await SqlAlchemyAnalysisRepository(session).record_feedback(row.id, row.user_id, score)
            is FeedbackOutcome.INVALID_SCORE
        )


@pytest.mark.parametrize("status", ["draft", "processing", "failed"])
async def test_report_deletion_rejects_non_completed_without_mutation(
    postgres_m4: async_sessionmaker[AsyncSession], status: str
) -> None:
    row = await create_row(postgres_m4, status)
    async with postgres_m4() as session:
        outcome = await SqlAlchemyAnalysisRepository(session).delete_owned(row.id, row.user_id)
        stored = await session.get(Analysis, row.id)
        assert outcome is DeletionOutcome.NOT_COMPLETED and stored and stored.status == status


async def test_completed_deletion_uses_sql_null_and_is_idempotent(
    postgres_m4: async_sessionmaker[AsyncSession],
) -> None:
    row = await create_row(postgres_m4)
    async with postgres_m4() as session:
        repository = SqlAlchemyAnalysisRepository(session)
        assert await repository.delete_owned(row.id, row.user_id) is DeletionOutcome.DELETED
        assert await repository.delete_owned(row.id, row.user_id) is DeletionOutcome.ALREADY_DELETED
        values = (
            await session.execute(
                text(
                    "SELECT normalized_conversation_json IS NULL, participants_json IS NULL, "
                    "result_json IS NULL, feedback_score IS NULL FROM analyses WHERE id=:id"
                ),
                {"id": row.id},
            )
        ).one()
        assert values == (True, True, True, True)


async def test_history_is_owned_newest_first_bounded_and_excludes_statuses(
    postgres_m4: async_sessionmaker[AsyncSession],
) -> None:
    first = await create_row(postgres_m4)
    owner = first.user_id
    now = datetime.now(UTC)
    for index in range(10):
        await create_row(postgres_m4, user_id=owner, completed_at=now + timedelta(seconds=index))
    await create_row(postgres_m4, "failed", owner)
    await create_row(postgres_m4)
    async with postgres_m4() as session:
        rows, has_next = await SqlAlchemyAnalysisRepository(session).list_completed(owner, 0)
        assert len(rows) == 8 and has_next
        assert all(row.user_id == owner and row.status == "completed" for row in rows)
        times = [row.completed_at for row in rows]
        assert all(value is not None for value in times)
        assert times == sorted((value for value in times if value is not None), reverse=True)


async def test_wrong_owner_cannot_delete_or_feedback(
    postgres_m4: async_sessionmaker[AsyncSession],
) -> None:
    row = await create_row(postgres_m4)
    async with postgres_m4() as session:
        repository = SqlAlchemyAnalysisRepository(session)
        assert await repository.delete_owned(row.id, uuid4()) is DeletionOutcome.NOT_FOUND
        assert await repository.record_feedback(row.id, uuid4(), 5) is FeedbackOutcome.NOT_FOUND


async def test_intake_cancellation_uses_sql_null_and_preserves_nested_json_null_before_clear(
    postgres_m4: async_sessionmaker[AsyncSession],
) -> None:
    row = await create_row(postgres_m4, "draft")
    async with postgres_m4() as session:
        stored = await session.get(Analysis, row.id)
        assert stored is not None
        stored.intake_step = "waiting_for_participant"
        stored.normalized_conversation_json = [
            {"id": "m1", "speaker": "A", "timestamp": None, "text": "PRIVATE", "source_order": 1}
        ]
        await session.commit()
        await session.refresh(stored)
        assert stored.normalized_conversation_json is not None
        assert stored.normalized_conversation_json[0]["timestamp"] is None
        await SqlAlchemyAnalysisRepository(session).cancel(stored)
        values = (
            await session.execute(
                text(
                    "SELECT normalized_conversation_json IS NULL, participants_json IS NULL "
                    "FROM analyses WHERE id=:id"
                ),
                {"id": row.id},
            )
        ).one()
        assert values == (True, True)
