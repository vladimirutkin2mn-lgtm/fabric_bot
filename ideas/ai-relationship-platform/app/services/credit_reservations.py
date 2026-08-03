"""Credit holds for refunds, serialized by the user's row.

Every mutating operation locks in this order: User, then RefundRequest/Reservation,
then reads the append-only ledger. CreditsService uses the same first lock, so
spend and reservation cannot race.
"""

from dataclasses import dataclass
from enum import StrEnum
from uuid import UUID

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.db.models import CreditReservation, CreditTransaction, RefundRequest, User


class ReservationOutcome(StrEnum):
    RESERVED = "reserved"
    ALREADY_RESERVED = "already_reserved"
    INSUFFICIENT_BALANCE = "insufficient_balance"
    RELEASED = "released"
    CONSUMED = "consumed"
    ALREADY_TERMINAL = "already_terminal"
    NOT_FOUND = "not_found"
    INVALID = "invalid"


@dataclass(frozen=True)
class CreditBalance:
    ledger: int
    reserved: int
    available: int


class CreditReservationService:
    def __init__(self, sessions: async_sessionmaker[AsyncSession]) -> None:
        self._sessions = sessions

    async def _balance(self, session: AsyncSession, user_id: UUID) -> CreditBalance:
        ledger = int(
            await session.scalar(
                select(func.coalesce(func.sum(CreditTransaction.amount), 0)).where(
                    CreditTransaction.user_id == user_id
                )
            )
            or 0
        )
        reserved = int(
            await session.scalar(
                select(func.coalesce(func.sum(CreditReservation.credit_units), 0)).where(
                    CreditReservation.user_id == user_id, CreditReservation.status == "active"
                )
            )
            or 0
        )
        return CreditBalance(ledger, reserved, max(0, ledger - reserved))

    async def balance(self, user_id: UUID) -> CreditBalance:
        async with self._sessions() as session:
            return await self._balance(session, user_id)

    async def reserve_for_refund(
        self, user_id: UUID, refund_request_id: UUID, credit_units: int
    ) -> ReservationOutcome:
        if credit_units < 1:
            return ReservationOutcome.INVALID
        async with self._sessions.begin() as session:
            if (
                await session.scalar(select(User.id).where(User.id == user_id).with_for_update())
                is None
            ):
                return ReservationOutcome.NOT_FOUND
            request = await session.scalar(
                select(RefundRequest)
                .where(RefundRequest.id == refund_request_id, RefundRequest.user_id == user_id)
                .with_for_update()
            )
            if request is None or request.credit_units != credit_units:
                return ReservationOutcome.INVALID
            existing = await session.scalar(
                select(CreditReservation).where(
                    CreditReservation.refund_request_id == refund_request_id
                )
            )
            if existing is not None:
                return ReservationOutcome.ALREADY_RESERVED
            if (await self._balance(session, user_id)).available < credit_units:
                return ReservationOutcome.INSUFFICIENT_BALANCE
            session.add(
                CreditReservation(
                    user_id=user_id,
                    refund_request_id=refund_request_id,
                    credit_units=credit_units,
                    status="active",
                )
            )
            request.status = "credits_reserved"
            return ReservationOutcome.RESERVED

    async def _finish(
        self, user_id: UUID, refund_request_id: UUID, target: str
    ) -> ReservationOutcome:
        async with self._sessions.begin() as session:
            if (
                await session.scalar(select(User.id).where(User.id == user_id).with_for_update())
                is None
            ):
                return ReservationOutcome.NOT_FOUND
            row = await session.scalar(
                select(CreditReservation)
                .where(
                    CreditReservation.refund_request_id == refund_request_id,
                    CreditReservation.user_id == user_id,
                )
                .with_for_update()
            )
            if row is None:
                return ReservationOutcome.NOT_FOUND
            if row.status != "active":
                return ReservationOutcome.ALREADY_TERMINAL
            row.status = target
            return (
                ReservationOutcome.CONSUMED if target == "consumed" else ReservationOutcome.RELEASED
            )

    async def consume(self, user_id: UUID, refund_request_id: UUID) -> ReservationOutcome:
        return await self._finish(user_id, refund_request_id, "consumed")

    async def release(self, user_id: UUID, refund_request_id: UUID) -> ReservationOutcome:
        return await self._finish(user_id, refund_request_id, "released")
