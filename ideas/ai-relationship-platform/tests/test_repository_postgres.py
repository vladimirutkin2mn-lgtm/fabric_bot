"""PostgreSQL integration coverage for the real user repository."""

import asyncio
import os
from collections.abc import AsyncIterator
from datetime import UTC, datetime
from uuid import UUID, uuid4

import pytest
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from app.db.base import Base
from app.db.models import Analysis, User
from app.providers.analytics import NoOpAnalyticsClient
from app.repositories.analyses import SqlAlchemyAnalysisRepository
from app.repositories.users import SqlAlchemyUserRepository
from app.services.conversation_intake import ConversationIntakeService, InvalidTransition
from app.services.conversation_parser import ConversationParser

pytestmark = pytest.mark.postgres


@pytest.fixture
async def postgres() -> AsyncIterator[tuple[AsyncEngine, async_sessionmaker[AsyncSession]]]:
    url = os.getenv("TEST_DATABASE_URL")
    if not url:
        pytest.skip("TEST_DATABASE_URL is required for PostgreSQL integration tests")
    engine = create_async_engine(url)
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.drop_all)
        await connection.run_sync(Base.metadata.create_all)
    yield engine, async_sessionmaker(engine, expire_on_commit=False)
    await engine.dispose()


async def test_get_or_create_persists_user_in_users_table(
    postgres: tuple[AsyncEngine, async_sessionmaker[AsyncSession]],
) -> None:
    _, sessions = postgres
    async with sessions() as session:
        user, created = await SqlAlchemyUserRepository(session).get_or_create(
            100, "anna", "Анна", "ru"
        )
        stored = await session.scalar(select(User).where(User.id == user.id))
    assert created
    assert stored is not None
    assert stored.telegram_user_id == 100


async def test_database_rejects_duplicate_telegram_user_id(
    postgres: tuple[AsyncEngine, async_sessionmaker[AsyncSession]],
) -> None:
    _, sessions = postgres
    async with sessions() as session:
        session.add_all(
            [
                User(telegram_user_id=101, first_name="Первый"),
                User(telegram_user_id=101, first_name="Б"),
            ]
        )
        with pytest.raises(IntegrityError):
            await session.commit()


async def test_concurrent_get_or_create_inserts_exactly_one_row(
    postgres: tuple[AsyncEngine, async_sessionmaker[AsyncSession]],
) -> None:
    _, sessions = postgres

    async def create() -> bool:
        async with sessions() as session:
            _, created = await SqlAlchemyUserRepository(session).get_or_create(
                102, "parallel", "Параллель", "ru"
            )
            return created

    created = await asyncio.gather(*(create() for _ in range(12)))
    async with sessions() as session:
        count = await session.scalar(select(func.count()).select_from(User))
    assert sum(created) == 1
    assert count == 1


async def test_save_persists_all_onboarding_fields(
    postgres: tuple[AsyncEngine, async_sessionmaker[AsyncSession]],
) -> None:
    _, sessions = postgres
    confirmed_at = datetime.now(UTC)
    accepted_at = datetime.now(UTC)
    async with sessions() as session:
        repository = SqlAlchemyUserRepository(session)
        user, _ = await repository.get_or_create(103, None, "Ирина", None)
        user.age_confirmed = True
        user.age_confirmed_at = confirmed_at
        user.consent_version = "1.0"
        user.consent_accepted_at = accepted_at
        user.onboarding_completed = True
        await repository.save(user)
    async with sessions() as session:
        stored = await session.scalar(select(User).where(User.telegram_user_id == 103))
    assert stored is not None
    assert stored.age_confirmed
    assert stored.age_confirmed_at == confirmed_at
    assert stored.consent_version == "1.0"
    assert stored.consent_accepted_at == accepted_at
    assert stored.onboarding_completed


async def test_repeated_get_or_create_updates_telegram_profile(
    postgres: tuple[AsyncEngine, async_sessionmaker[AsyncSession]],
) -> None:
    _, sessions = postgres
    async with sessions() as session:
        repository = SqlAlchemyUserRepository(session)
        original, created = await repository.get_or_create(104, "old", "Старое", "en")
        updated, created_again = await repository.get_or_create(104, "new", "Новое", "ru")
    assert created
    assert not created_again
    assert updated.id == original.id
    assert (updated.telegram_username, updated.first_name, updated.telegram_language) == (
        "new",
        "Новое",
        "ru",
    )


async def _persist_user(sessions: async_sessionmaker[AsyncSession], telegram_id: int) -> User:
    async with sessions() as session:
        user, _ = await SqlAlchemyUserRepository(session).get_or_create(
            telegram_id, None, "Test", "ru"
        )
        return user


async def test_analysis_json_relationship_ownership_and_durable_transition(
    postgres: tuple[AsyncEngine, async_sessionmaker[AsyncSession]],
) -> None:
    _, sessions = postgres
    user = await _persist_user(sessions, 200)
    async with sessions() as session:
        repository = SqlAlchemyAnalysisRepository(session)
        analysis, created = await repository.create_or_resume(user.id)
        analysis.normalized_conversation_json = [
            {"id": "m1", "speaker": "A", "text": "private", "timestamp": None, "source_order": 1}
        ]
        analysis.participants_json = {"A": "Anna", "B": "Ivan"}
        analysis.message_count = 1
        analysis.character_count = 7
        analysis.intake_step = "waiting_for_participant"
        await repository.save(analysis)
        analysis_id = analysis.id
    async with sessions() as session:
        repository = SqlAlchemyAnalysisRepository(session)
        stored = await repository.get_owned(analysis_id, user.id)
        assert created and stored is not None
        assert stored.normalized_conversation_json is not None
        assert stored.participants_json == {"A": "Anna", "B": "Ivan"}
        assert stored.user_id == user.id
        await session.refresh(stored, ["user"])
        assert stored.user.id == user.id
        assert await repository.get_owned(analysis_id, uuid4()) is None


async def test_concurrent_analysis_creation_has_one_active_draft(
    postgres: tuple[AsyncEngine, async_sessionmaker[AsyncSession]],
) -> None:
    _, sessions = postgres
    user = await _persist_user(sessions, 201)

    async def create() -> UUID:
        async with sessions() as session:
            analysis, _ = await SqlAlchemyAnalysisRepository(session).create_or_resume(user.id)
            return analysis.id

    ids = await asyncio.gather(*(create() for _ in range(10)))
    assert len(set(ids)) == 1
    async with sessions() as session:
        count = await session.scalar(
            select(func.count())
            .select_from(Analysis)
            .where(
                Analysis.user_id == user.id,
                Analysis.status == "draft",
                Analysis.intake_step != "complete",
            )
        )
    assert count == 1


async def test_cancel_and_new_draft_after_completed_intake(
    postgres: tuple[AsyncEngine, async_sessionmaker[AsyncSession]],
) -> None:
    _, sessions = postgres
    user = await _persist_user(sessions, 202)
    async with sessions() as session:
        repository = SqlAlchemyAnalysisRepository(session)
        first, _ = await repository.create_or_resume(user.id)
        first.intake_step = "complete"
        await repository.save(first)
        second, created = await repository.create_or_resume(user.id)
        assert created and second.id != first.id
        await repository.cancel(second)
        assert second.status == "deleted" and await repository.get_active(user.id) is None


async def test_analysis_constraints_reject_invalid_status_and_duplicate_active(
    postgres: tuple[AsyncEngine, async_sessionmaker[AsyncSession]],
) -> None:
    _, sessions = postgres
    user = await _persist_user(sessions, 203)
    async with sessions() as session:
        session.add(
            Analysis(user_id=user.id, status="invalid", intake_step="waiting_for_conversation")
        )
        with pytest.raises(IntegrityError):
            await session.commit()
    async with sessions() as session:
        session.add_all(
            [
                Analysis(user_id=user.id, intake_step="waiting_for_conversation"),
                Analysis(user_id=user.id, intake_step="waiting_for_goal"),
            ]
        )
        with pytest.raises(IntegrityError):
            await session.commit()


async def test_reset_persists_cleared_sensitive_fields(
    postgres: tuple[AsyncEngine, async_sessionmaker[AsyncSession]],
) -> None:
    _, sessions = postgres
    user = await _persist_user(sessions, 204)
    async with sessions() as session:
        repository = SqlAlchemyAnalysisRepository(session)
        service = ConversationIntakeService(repository, ConversationParser(), NoOpAnalyticsClient())
        draft, _ = await repository.create_or_resume(user.id)
        await service.submit(draft, "A: one\nB: two\nA: three\nB: four")
        await service.participant(draft, "A")
        await service.goal(draft, "Question")
        await service.reset_conversation(draft)
        draft_id = draft.id
    async with sessions() as session:
        stored = await SqlAlchemyAnalysisRepository(session).get_owned(draft_id, user.id)
        assert stored is not None and stored.intake_step == "waiting_for_conversation"
        assert stored.normalized_conversation_json is None and stored.participants_json is None
        assert stored.user_goal is None and stored.user_participant_label is None


async def test_stale_callbacks_cannot_mutate_completed_analysis_with_active_successor(
    postgres: tuple[AsyncEngine, async_sessionmaker[AsyncSession]],
) -> None:
    _, sessions = postgres
    user = await _persist_user(sessions, 205)
    async with sessions() as session:
        repository = SqlAlchemyAnalysisRepository(session)
        service = ConversationIntakeService(repository, ConversationParser(), NoOpAnalyticsClient())
        completed, _ = await repository.create_or_resume(user.id)
        completed.intake_step = "complete"
        await repository.save(completed)
        active, created = await repository.create_or_resume(user.id)
        assert created
        with pytest.raises(InvalidTransition):
            await service.reset_conversation(completed)
        with pytest.raises(InvalidTransition):
            await service.cancel(completed)
        completed_id, active_id = completed.id, active.id
    async with sessions() as session:
        repository = SqlAlchemyAnalysisRepository(session)
        stored_completed = await repository.get_owned(completed_id, user.id)
        stored_active = await repository.get_owned(active_id, user.id)
        assert stored_completed is not None
        assert stored_completed.status == "draft" and stored_completed.intake_step == "complete"
        assert stored_active is not None
        assert stored_active.status == "draft"
        assert stored_active.intake_step == "waiting_for_conversation"
        active_draft = await repository.get_active(user.id)
        assert active_draft is not None and active_draft.id == active_id
