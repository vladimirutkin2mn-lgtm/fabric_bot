"""Encrypted durable aiogram FSM and distributed event-isolation tests."""

import asyncio
from typing import cast

import pytest
from aiogram.fsm.storage.base import StorageKey
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from app.bot.postgres_fsm import PostgresEventIsolation, PostgresFSMStorage
from app.db.fsm_models import TelegramFSMState
from app.services.sensitive_content import AESGCMSensitiveContentCipher

pytestmark = pytest.mark.postgres


def storage(sessions: async_sessionmaker[AsyncSession]) -> PostgresFSMStorage:
    return PostgresFSMStorage(
        sessions,
        AESGCMSensitiveContentCipher(b"postgres-fsm-test-key"),
    )


def key(user_id: int = 42) -> StorageKey:
    return StorageKey(bot_id=123456789, chat_id=user_id, user_id=user_id)


async def test_state_and_encrypted_data_survive_storage_recreation(
    payment_db: async_sessionmaker[AsyncSession],
) -> None:
    first = storage(payment_db)
    await first.set_state(key(), "IntakeStates:waiting_for_goal")
    await first.set_data(key(), {"analysis_id": "private-analysis-id", "step": 3})

    second = storage(payment_db)
    assert await second.get_state(key()) == "IntakeStates:waiting_for_goal"
    assert await second.get_data(key()) == {
        "analysis_id": "private-analysis-id",
        "step": 3,
    }

    async with payment_db() as session:
        row = await session.scalar(select(TelegramFSMState))
        assert row is not None and row.data_ciphertext is not None
        assert b"private-analysis-id" not in row.data_ciphertext


async def test_clear_removes_empty_fsm_record(
    payment_db: async_sessionmaker[AsyncSession],
) -> None:
    fsm = storage(payment_db)
    await fsm.set_state(key(), "OnboardingStates:waiting_for_age")
    await fsm.set_data(key(), {"temporary": True})
    await fsm.set_state(key(), None)
    await fsm.set_data(key(), {})

    assert await fsm.get_state(key()) is None
    assert await fsm.get_data(key()) == {}
    async with payment_db() as session:
        assert await session.scalar(select(TelegramFSMState)) is None


async def test_concurrent_update_data_does_not_lose_keys(
    payment_db: async_sessionmaker[AsyncSession],
) -> None:
    fsm = storage(payment_db)

    async def add(index: int) -> None:
        await fsm.update_data(key(), {f"key-{index}": index})

    await asyncio.gather(*(add(index) for index in range(20)))
    assert await fsm.get_data(key()) == {f"key-{index}": index for index in range(20)}


async def test_storage_keys_are_isolated(
    payment_db: async_sessionmaker[AsyncSession],
) -> None:
    fsm = storage(payment_db)
    await fsm.set_state(key(42), "state-42")
    await fsm.set_state(key(43), "state-43")
    await fsm.set_data(key(42), {"owner": 42})
    await fsm.set_data(key(43), {"owner": 43})

    assert await fsm.get_state(key(42)) == "state-42"
    assert await fsm.get_state(key(43)) == "state-43"
    assert await fsm.get_data(key(42)) == {"owner": 42}
    assert await fsm.get_data(key(43)) == {"owner": 43}


async def test_event_isolation_serializes_same_key_across_instances(
    payment_db: async_sessionmaker[AsyncSession],
) -> None:
    engine = cast(AsyncEngine, payment_db.kw["bind"])
    first = PostgresEventIsolation(engine)
    second = PostgresEventIsolation(engine)
    first_entered = asyncio.Event()
    release_first = asyncio.Event()
    order: list[str] = []

    async def hold_first() -> None:
        async with first.lock(key()):
            order.append("first")
            first_entered.set()
            await asyncio.wait_for(release_first.wait(), timeout=5)

    async def wait_second() -> None:
        await asyncio.wait_for(first_entered.wait(), timeout=5)
        async with second.lock(key()):
            order.append("second")

    first_task = asyncio.create_task(hold_first())
    second_task = asyncio.create_task(wait_second())
    await asyncio.wait_for(first_entered.wait(), timeout=5)
    await asyncio.sleep(0.1)
    assert order == ["first"]
    release_first.set()
    await asyncio.gather(first_task, second_task)
    assert order == ["first", "second"]


async def test_event_isolation_allows_different_keys_in_parallel(
    payment_db: async_sessionmaker[AsyncSession],
) -> None:
    engine = cast(AsyncEngine, payment_db.kw["bind"])
    isolation = PostgresEventIsolation(engine)
    both_entered = asyncio.Event()
    entered = 0
    guard = asyncio.Lock()

    async def enter(user_id: int) -> None:
        nonlocal entered
        async with isolation.lock(key(user_id)):
            async with guard:
                entered += 1
                if entered == 2:
                    both_entered.set()
            await asyncio.wait_for(both_entered.wait(), timeout=5)

    await asyncio.gather(enter(42), enter(43))
    assert entered == 2
