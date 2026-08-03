"""Transactional, append-only credit ledger operations."""

from dataclasses import dataclass
from enum import StrEnum
from uuid import UUID

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.db.models import Analysis, CreditTransaction, User


class SpendOutcome(StrEnum):
    SPENT = "spent"
    ALREADY_SPENT_ACTIVE = "already_spent_active"
    ALREADY_SPENT = "already_spent_active"
    ALREADY_SPENT_REFUNDED = "already_spent_refunded"
    INSUFFICIENT_BALANCE = "insufficient_balance"
    ANALYSIS_NOT_FOUND = "analysis_not_found"
    INVALID_AMOUNT = "invalid_amount"


class RefundOutcome(StrEnum):
    REFUNDED = "refunded"
    ALREADY_REFUNDED = "already_refunded"
    SPEND_NOT_FOUND = "spend_not_found"
    INVALID_SPEND = "invalid_spend"
    AUTHORIZATION_MISMATCH = "authorization_mismatch"


class GrantOutcome(StrEnum):
    GRANTED = "granted"
    ALREADY_GRANTED = "already_granted"
    USER_NOT_FOUND = "user_not_found"
    INVALID_AMOUNT = "invalid_amount"


@dataclass(frozen=True)
class SpendResult:
    outcome: SpendOutcome
    transaction_id: UUID | None = None
    balance: int = 0


class CreditsService:
    """Serialize each user's debits by locking their durable user row."""

    def __init__(self, sessions: async_sessionmaker[AsyncSession]) -> None:
        self._sessions = sessions

    async def balance(self, user_id: UUID) -> int:
        async with self._sessions() as session:
            return int(
                await session.scalar(
                    select(func.coalesce(func.sum(CreditTransaction.amount), 0)).where(
                        CreditTransaction.user_id == user_id
                    )
                )
                or 0
            )

    async def spend(self, user_id: UUID, analysis_id: UUID, amount: int) -> SpendResult:
        if amount < 1:
            return SpendResult(SpendOutcome.INVALID_AMOUNT)
        key = f"analysis_full_access:{analysis_id}"
        async with self._sessions.begin() as session:
            if (
                await session.scalar(select(User.id).where(User.id == user_id).with_for_update())
                is None
            ):
                return SpendResult(SpendOutcome.ANALYSIS_NOT_FOUND)
            analysis = await session.scalar(
                select(Analysis).where(Analysis.id == analysis_id, Analysis.user_id == user_id)
            )
            if analysis is None:
                return SpendResult(SpendOutcome.ANALYSIS_NOT_FOUND)
            existing = await session.scalar(
                select(CreditTransaction).where(CreditTransaction.idempotency_key == key)
            )
            balance = int(
                await session.scalar(
                    select(func.coalesce(func.sum(CreditTransaction.amount), 0)).where(
                        CreditTransaction.user_id == user_id
                    )
                )
                or 0
            )
            if existing is not None:
                if not (
                    existing.user_id == user_id
                    and existing.analysis_id == analysis_id
                    and existing.type == "spend"
                    and existing.amount == -amount
                ):
                    return SpendResult(SpendOutcome.ANALYSIS_NOT_FOUND, balance=balance)
                refunded = await session.scalar(
                    select(CreditTransaction.id).where(
                        CreditTransaction.reverses_transaction_id == existing.id
                    )
                )
                if refunded is not None:
                    return SpendResult(SpendOutcome.ALREADY_SPENT_REFUNDED, balance=balance)
                return SpendResult(SpendOutcome.ALREADY_SPENT_ACTIVE, existing.id, balance)
            if balance < amount:
                return SpendResult(SpendOutcome.INSUFFICIENT_BALANCE, balance=balance)
            row = CreditTransaction(
                user_id=user_id,
                type="spend",
                amount=-amount,
                idempotency_key=key,
                analysis_id=analysis_id,
            )
            session.add(row)
            await session.flush()
            return SpendResult(SpendOutcome.SPENT, row.id, balance - amount)

    async def refund(self, user_id: UUID, analysis_id: UUID, spend_id: UUID) -> RefundOutcome:
        async with self._sessions.begin() as session:
            spend = await session.scalar(
                select(CreditTransaction).where(CreditTransaction.id == spend_id).with_for_update()
            )
            if spend is None:
                return RefundOutcome.SPEND_NOT_FOUND
            if spend.user_id != user_id or spend.analysis_id != analysis_id:
                return RefundOutcome.AUTHORIZATION_MISMATCH
            if spend.type != "spend" or spend.amount >= 0:
                return RefundOutcome.INVALID_SPEND
            key = f"refund:{spend.id}"
            if (
                await session.scalar(
                    select(CreditTransaction.id).where(CreditTransaction.idempotency_key == key)
                )
                is not None
            ):
                return RefundOutcome.ALREADY_REFUNDED
            session.add(
                CreditTransaction(
                    user_id=spend.user_id,
                    type="refund",
                    amount=-spend.amount,
                    idempotency_key=key,
                    analysis_id=spend.analysis_id,
                    reverses_transaction_id=spend.id,
                )
            )
            return RefundOutcome.REFUNDED

    async def grant(self, user_id: UUID, amount: int, key: str) -> GrantOutcome:
        if amount < 1:
            return GrantOutcome.INVALID_AMOUNT
        async with self._sessions.begin() as session:
            if (
                await session.scalar(select(User.id).where(User.id == user_id).with_for_update())
                is None
            ):
                return GrantOutcome.USER_NOT_FOUND
            if (
                await session.scalar(
                    select(CreditTransaction.id).where(CreditTransaction.idempotency_key == key)
                )
                is not None
            ):
                return GrantOutcome.ALREADY_GRANTED
            session.add(
                CreditTransaction(user_id=user_id, type="grant", amount=amount, idempotency_key=key)
            )
            return GrantOutcome.GRANTED

    async def adjustment(self, user_id: UUID, amount: int, key: str) -> GrantOutcome:
        if amount == 0:
            return GrantOutcome.INVALID_AMOUNT
        async with self._sessions.begin() as session:
            if (
                await session.scalar(select(User.id).where(User.id == user_id).with_for_update())
                is None
            ):
                return GrantOutcome.USER_NOT_FOUND
            if (
                await session.scalar(
                    select(CreditTransaction.id).where(CreditTransaction.idempotency_key == key)
                )
                is not None
            ):
                return GrantOutcome.ALREADY_GRANTED
            balance = int(
                await session.scalar(
                    select(func.coalesce(func.sum(CreditTransaction.amount), 0)).where(
                        CreditTransaction.user_id == user_id
                    )
                )
                or 0
            )
            if balance + amount < 0:
                return GrantOutcome.INVALID_AMOUNT
            session.add(
                CreditTransaction(
                    user_id=user_id, type="adjustment", amount=amount, idempotency_key=key
                )
            )
            return GrantOutcome.GRANTED
