"""PostgreSQL authorization and concurrency regressions for the credit ledger."""

import asyncio
import os
from collections.abc import AsyncIterator
from uuid import UUID, uuid4

import pytest
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.db.base import Base
from app.db.models import Analysis, CreditTransaction, User
from app.services.credits_service import CreditsService, RefundOutcome, SpendOutcome

pytestmark = pytest.mark.postgres


@pytest.fixture
async def billing_db() -> AsyncIterator[async_sessionmaker[AsyncSession]]:
    url = os.getenv("TEST_DATABASE_URL")
    if not url:
        pytest.skip("TEST_DATABASE_URL is required")
    engine = create_async_engine(url)
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.drop_all)
        await connection.run_sync(Base.metadata.create_all)
    yield async_sessionmaker(engine, expire_on_commit=False)
    await engine.dispose()


async def _user_and_analyses(
    sessions: async_sessionmaker[AsyncSession], count: int
) -> tuple[UUID, list[UUID]]:
    async with sessions.begin() as session:
        user = User(telegram_user_id=uuid4().int % 10**12, first_name="Fictional")
        session.add(user)
        await session.flush()
        rows = [
            Analysis(user_id=user.id, status="draft", intake_step="complete") for _ in range(count)
        ]
        session.add_all(rows)
        await session.flush()
        return user.id, [row.id for row in rows]


async def test_same_analysis_spend_and_refund_are_exactly_once(
    billing_db: async_sessionmaker[AsyncSession],
) -> None:
    user_id, analyses = await _user_and_analyses(billing_db, 1)
    service = CreditsService(billing_db)
    await service.grant(user_id, 1, "test:grant")
    spends = await asyncio.gather(*(service.spend(user_id, analyses[0], 1) for _ in range(10)))
    assert [item.outcome for item in spends].count(SpendOutcome.SPENT) == 1
    assert [item.outcome for item in spends].count(SpendOutcome.ALREADY_SPENT) == 9
    spend_id = next(item.transaction_id for item in spends if item.transaction_id is not None)
    assert spend_id is not None
    refunds = await asyncio.gather(
        *(service.refund(user_id, analyses[0], spend_id) for _ in range(10))
    )
    assert refunds.count(RefundOutcome.REFUNDED) == 1
    assert refunds.count(RefundOutcome.ALREADY_REFUNDED) == 9
    assert await service.balance(user_id) == 1


async def test_different_spends_never_make_balance_negative(
    billing_db: async_sessionmaker[AsyncSession],
) -> None:
    user_id, analyses = await _user_and_analyses(billing_db, 10)
    service = CreditsService(billing_db)
    await service.grant(user_id, 1, "test:grant")
    results = await asyncio.gather(
        *(service.spend(user_id, analysis_id, 1) for analysis_id in analyses)
    )
    assert [item.outcome for item in results].count(SpendOutcome.SPENT) == 1
    assert [item.outcome for item in results].count(SpendOutcome.INSUFFICIENT_BALANCE) == 9
    assert await service.balance(user_id) == 0


async def test_cross_user_spend_never_exposes_or_refunds_owner_transaction(
    billing_db: async_sessionmaker[AsyncSession],
) -> None:
    owner, analyses = await _user_and_analyses(billing_db, 1)
    attacker, _ = await _user_and_analyses(billing_db, 1)
    service = CreditsService(billing_db)
    await service.grant(owner, 1, "owner:grant")
    original = await service.spend(owner, analyses[0], 1)
    attack = await service.spend(attacker, analyses[0], 1)
    assert attack.outcome is SpendOutcome.ANALYSIS_NOT_FOUND and attack.transaction_id is None
    assert original.transaction_id is not None
    assert (
        await service.refund(attacker, analyses[0], original.transaction_id)
        is RefundOutcome.AUTHORIZATION_MISMATCH
    )
    async with billing_db() as session:
        refunds = await session.scalar(
            select(func.count())
            .select_from(CreditTransaction)
            .where(CreditTransaction.type == "refund")
        )
    assert refunds == 0 and await service.balance(owner) == 0
