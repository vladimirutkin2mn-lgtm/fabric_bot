"""Real-PostgreSQL concurrency regressions for refund credit reservations."""

import asyncio
import os
from collections.abc import AsyncIterator
from uuid import UUID, uuid4

import pytest
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.db.base import Base
from app.db.models import (
    Analysis,
    CreditReservation,
    PaymentOrder,
    RefundRequest,
    User,
)
from app.services.credit_reservations import CreditReservationService, ReservationOutcome
from app.services.credits_service import CreditsService, SpendOutcome

pytestmark = pytest.mark.postgres


@pytest.fixture
async def reservation_db() -> AsyncIterator[async_sessionmaker[AsyncSession]]:
    url = os.getenv("TEST_DATABASE_URL")
    if not url:
        pytest.skip("TEST_DATABASE_URL is required")
    engine = create_async_engine(url)
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.drop_all)
        await connection.run_sync(Base.metadata.create_all)
    yield async_sessionmaker(engine, expire_on_commit=False)
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.drop_all)
    await engine.dispose()


async def _billing_rows(
    sessions: async_sessionmaker[AsyncSession], refund_count: int
) -> tuple[UUID, UUID, list[UUID]]:
    async with sessions.begin() as session:
        user = User(telegram_user_id=uuid4().int % 10**12, first_name="Billing")
        session.add(user)
        await session.flush()
        analysis = Analysis(user_id=user.id, status="draft", intake_step="complete")
        order = PaymentOrder(
            user_id=user.id,
            provider="mock",
            product_code="analysis_pack_5",
            status="pending",
            credits=5,
            amount_minor=500,
            currency="RUB",
            commercial_snapshot={
                "product_code": "analysis_pack_5",
                "product_version": 1,
                "credits": 5,
                "amount_minor": 500,
                "currency": "RUB",
                "provider": "mock",
                "market": "RU",
                "billing_period": None,
            },
        )
        session.add_all([analysis, order])
        await session.flush()
        refunds = [
            RefundRequest(
                user_id=user.id,
                payment_order_id=order.id,
                provider="mock",
                amount_minor=500,
                currency="RUB",
                credit_units=5,
                reason="requested_by_user",
                idempotency_key=f"refund:{uuid4()}",
            )
            for _ in range(refund_count)
        ]
        session.add_all(refunds)
        await session.flush()
        return user.id, analysis.id, [row.id for row in refunds]


async def _reservation_count(sessions: async_sessionmaker[AsyncSession]) -> int:
    async with sessions() as session:
        return int(await session.scalar(select(func.count()).select_from(CreditReservation)) or 0)


async def test_ten_reservations_for_all_five_credits_have_one_winner(
    reservation_db: async_sessionmaker[AsyncSession],
) -> None:
    user_id, _, refund_ids = await _billing_rows(reservation_db, 10)
    await CreditsService(reservation_db).grant(user_id, 5, "reservation:grant")
    service = CreditReservationService(reservation_db)
    outcomes = await asyncio.gather(
        *(service.reserve_for_refund(user_id, refund_id, 5) for refund_id in refund_ids)
    )
    assert outcomes.count(ReservationOutcome.RESERVED) == 1
    assert outcomes.count(ReservationOutcome.INSUFFICIENT_BALANCE) == 9
    assert await _reservation_count(reservation_db) == 1
    assert (await service.balance(user_id)).available == 0


async def test_ten_calls_for_same_refund_create_one_reservation(
    reservation_db: async_sessionmaker[AsyncSession],
) -> None:
    user_id, _, refund_ids = await _billing_rows(reservation_db, 1)
    await CreditsService(reservation_db).grant(user_id, 5, "same-refund:grant")
    service = CreditReservationService(reservation_db)
    outcomes = await asyncio.gather(
        *(service.reserve_for_refund(user_id, refund_ids[0], 5) for _ in range(10))
    )
    assert outcomes.count(ReservationOutcome.RESERVED) == 1
    assert outcomes.count(ReservationOutcome.ALREADY_RESERVED) == 9
    assert await _reservation_count(reservation_db) == 1


async def test_spend_vs_reserve_race_never_overdraws_available_balance(
    reservation_db: async_sessionmaker[AsyncSession],
) -> None:
    reservations = CreditReservationService(reservation_db)
    credits = CreditsService(reservation_db)
    for iteration in range(25):
        user_id, analysis_id, refund_ids = await _billing_rows(reservation_db, 1)
        await credits.grant(user_id, 5, f"spend-reserve:{iteration}")
        reserve, spend = await asyncio.gather(
            reservations.reserve_for_refund(user_id, refund_ids[0], 5),
            credits.spend(user_id, analysis_id, 5),
        )
        assert (reserve is ReservationOutcome.RESERVED) != (spend.outcome is SpendOutcome.SPENT)
        balance = await reservations.balance(user_id)
        assert balance.available >= 0
        assert balance.available == 0


@pytest.mark.parametrize(
    ("operation", "winner"),
    [("release", ReservationOutcome.RELEASED), ("consume", ReservationOutcome.CONSUMED)],
)
async def test_concurrent_terminal_operation_has_one_effective_transition(
    reservation_db: async_sessionmaker[AsyncSession], operation: str, winner: ReservationOutcome
) -> None:
    user_id, _, refund_ids = await _billing_rows(reservation_db, 1)
    await CreditsService(reservation_db).grant(user_id, 5, f"terminal:{operation}")
    service = CreditReservationService(reservation_db)
    assert (
        await service.reserve_for_refund(user_id, refund_ids[0], 5) is ReservationOutcome.RESERVED
    )
    method = service.release if operation == "release" else service.consume
    outcomes = await asyncio.gather(*(method(user_id, refund_ids[0]) for _ in range(10)))
    assert outcomes.count(winner) == 1
    assert outcomes.count(ReservationOutcome.ALREADY_TERMINAL) == 9
