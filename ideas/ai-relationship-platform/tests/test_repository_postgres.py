"""PostgreSQL integration coverage for the real user repository."""

import asyncio
import os
from collections.abc import AsyncIterator
from datetime import UTC, datetime

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
from app.db.models import User
from app.repositories.users import SqlAlchemyUserRepository

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
