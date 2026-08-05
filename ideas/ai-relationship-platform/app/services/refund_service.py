"""Refund eligibility and atomic credit reservation.

New refund requests lock the user first, matching CreditsService and
CreditReservationService. This makes spend-versus-refund and two-refunds-for-one-purchase
serializable without holding a database transaction across provider I/O.
"""

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from enum import StrEnum
from uuid import UUID, uuid4

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.config import Settings
from app.db.models import (
    BillingJob,
    BillingOutboxEvent,
    CreditReservation,
    CreditTransaction,
    PaymentOrder,
    RefundRequest,
    User,
)
from app.providers.payments.refund_gateway import RefundGateway

_ACTIVE_REFUND_STATUSES = (
    "requested",
    "credits_reserved",
    "provider_pending",
    "manual_review",
    "succeeded",
)
_PENDING_REFUND_STATUSES = (
    "requested",
    "credits_reserved",
    "provider_pending",
    "manual_review",
)


class RefundRequestOutcome(StrEnum):
    CREATED = "created"
    DISABLED = "disabled"
    NOT_FOUND = "not_found"
    NOT_ELIGIBLE = "not_eligible"
    INVALID_UNITS = "invalid_units"
    INSUFFICIENT_CREDITS = "insufficient_credits"
    PARTIAL_UNSUPPORTED = "partial_unsupported"
    ALREADY_PENDING = "already_pending"


@dataclass(frozen=True)
class RefundPurchaseView:
    payment_order_id: UUID
    provider: str
    product_code: str
    refundable_credits: int
    refund_amount_minor: int
    currency: str
    completed_at: datetime


@dataclass(frozen=True)
class RefundView:
    id: UUID
    payment_order_id: UUID
    provider: str
    status: str
    amount_minor: int
    currency: str
    credit_units: int
    failure_code: str | None
    created_at: datetime


@dataclass(frozen=True)
class RefundRequestResult:
    outcome: RefundRequestOutcome
    refund: RefundView | None = None


class RefundService:
    def __init__(
        self,
        sessions: async_sessionmaker[AsyncSession],
        settings: Settings,
        gateways: dict[str, RefundGateway],
    ) -> None:
        self._sessions = sessions
        self._settings = settings
        self._gateways = gateways

    async def eligible_purchases(self, user_id: UUID) -> tuple[RefundPurchaseView, ...]:
        cutoff = datetime.now(UTC) - timedelta(days=self._settings.billing_refund_window_days)
        async with self._sessions() as session:
            user = await session.get(User, user_id)
            if user is None or user.privacy_status != "active":
                return ()
            available = await self._available_balance(session, user_id)
            if available < 1:
                return ()
            rows = (
                await session.execute(
                    select(PaymentOrder, CreditTransaction)
                    .join(
                        CreditTransaction,
                        (CreditTransaction.payment_order_id == PaymentOrder.id)
                        & (CreditTransaction.type == "purchase"),
                    )
                    .where(
                        PaymentOrder.user_id == user_id,
                        PaymentOrder.status == "completed",
                        PaymentOrder.completed_at.is_not(None),
                        PaymentOrder.completed_at >= cutoff,
                    )
                    .order_by(PaymentOrder.completed_at.desc())
                    .limit(20)
                )
            ).all()
            result: list[RefundPurchaseView] = []
            for order, purchase in rows:
                gateway = self._gateways.get(order.provider)
                if gateway is None or order.completed_at is None or order.provider_payment_id is None:
                    continue
                committed_credits, committed_amount = await self._committed(
                    session, order.id
                )
                remaining = purchase.amount - committed_credits
                if remaining < 1:
                    continue
                units = min(remaining, available)
                if not gateway.refund_capabilities.partial_refunds and (
                    committed_credits > 0 or units != order.credits
                ):
                    continue
                amount = self._refund_amount(
                    order,
                    units,
                    remaining,
                    committed_amount,
                )
                if amount < 1:
                    continue
                result.append(
                    RefundPurchaseView(
                        payment_order_id=order.id,
                        provider=order.provider,
                        product_code=order.product_code,
                        refundable_credits=units,
                        refund_amount_minor=amount,
                        currency=order.currency,
                        completed_at=order.completed_at,
                    )
                )
            return tuple(result)

    async def request_refund(
        self,
        user_id: UUID,
        payment_order_id: UUID,
        credit_units: int | None = None,
        reason: str = "requested_by_customer",
    ) -> RefundRequestResult:
        if not self._settings.permits_refund():
            return RefundRequestResult(RefundRequestOutcome.DISABLED)
        if credit_units is not None and credit_units < 1:
            return RefundRequestResult(RefundRequestOutcome.INVALID_UNITS)
        cutoff = datetime.now(UTC) - timedelta(days=self._settings.billing_refund_window_days)
        async with self._sessions.begin() as session:
            user = await session.scalar(
                select(User).where(User.id == user_id).with_for_update()
            )
            if user is None or user.privacy_status != "active":
                return RefundRequestResult(RefundRequestOutcome.NOT_FOUND)
            order = await session.scalar(
                select(PaymentOrder)
                .where(
                    PaymentOrder.id == payment_order_id,
                    PaymentOrder.user_id == user_id,
                )
                .with_for_update()
            )
            if (
                order is None
                or order.status != "completed"
                or order.completed_at is None
                or order.completed_at < cutoff
                or order.provider_payment_id is None
            ):
                return RefundRequestResult(RefundRequestOutcome.NOT_ELIGIBLE)
            gateway = self._gateways.get(order.provider)
            if gateway is None:
                return RefundRequestResult(RefundRequestOutcome.NOT_ELIGIBLE)
            purchase = await session.scalar(
                select(CreditTransaction)
                .where(
                    CreditTransaction.payment_order_id == order.id,
                    CreditTransaction.type == "purchase",
                )
                .with_for_update()
            )
            if purchase is None or purchase.amount != order.credits:
                return RefundRequestResult(RefundRequestOutcome.NOT_ELIGIBLE)
            pending = await session.scalar(
                select(RefundRequest.id).where(
                    RefundRequest.payment_order_id == order.id,
                    RefundRequest.status.in_(_PENDING_REFUND_STATUSES),
                )
            )
            if pending is not None:
                return RefundRequestResult(RefundRequestOutcome.ALREADY_PENDING)
            committed_credits, committed_amount = await self._committed(session, order.id)
            remaining = purchase.amount - committed_credits
            units = remaining if credit_units is None else credit_units
            if units < 1 or units > remaining:
                return RefundRequestResult(RefundRequestOutcome.INVALID_UNITS)
            if not gateway.refund_capabilities.partial_refunds and (
                committed_credits > 0 or units != order.credits
            ):
                return RefundRequestResult(RefundRequestOutcome.PARTIAL_UNSUPPORTED)
            if await self._available_balance(session, user_id) < units:
                return RefundRequestResult(RefundRequestOutcome.INSUFFICIENT_CREDITS)
            amount_minor = self._refund_amount(
                order,
                units,
                remaining,
                committed_amount,
            )
            if amount_minor < 1:
                return RefundRequestResult(RefundRequestOutcome.NOT_ELIGIBLE)
            refund_id = uuid4()
            idempotency_key = f"refund:{refund_id.hex}"
            refund = RefundRequest(
                id=refund_id,
                user_id=user_id,
                payment_order_id=order.id,
                provider=order.provider,
                status="credits_reserved",
                amount_minor=amount_minor,
                currency=order.currency,
                credit_units=units,
                reason=reason[:255],
                idempotency_key=idempotency_key,
            )
            session.add(refund)
            session.add(
                CreditReservation(
                    user_id=user_id,
                    refund_request_id=refund_id,
                    credit_units=units,
                    status="active",
                )
            )
            session.add(
                BillingJob(
                    job_type="refund_reconciliation",
                    provider=order.provider,
                    object_type="refund_request",
                    object_id=str(refund_id),
                    idempotency_key=f"refund:job:{refund_id}",
                    status="pending",
                )
            )
            session.add(
                BillingOutboxEvent(
                    aggregate_type="refund",
                    aggregate_id=str(refund_id),
                    event_type="refund_requested",
                    payload={
                        "product_code": order.product_code,
                        "credits": units,
                        "amount_minor": amount_minor,
                        "currency": order.currency,
                    },
                    idempotency_key=f"refund_requested:{refund_id}",
                )
            )
            await session.flush()
            return RefundRequestResult(
                RefundRequestOutcome.CREATED,
                self._view(refund),
            )

    async def history(self, user_id: UUID, limit: int = 10) -> tuple[RefundView, ...]:
        async with self._sessions() as session:
            rows = list(
                (
                    await session.scalars(
                        select(RefundRequest)
                        .where(RefundRequest.user_id == user_id)
                        .order_by(RefundRequest.created_at.desc())
                        .limit(max(1, min(limit, 50)))
                    )
                ).all()
            )
            return tuple(self._view(row) for row in rows)

    @staticmethod
    async def _committed(session: AsyncSession, order_id: UUID) -> tuple[int, int]:
        row = (
            await session.execute(
                select(
                    func.coalesce(func.sum(RefundRequest.credit_units), 0),
                    func.coalesce(func.sum(RefundRequest.amount_minor), 0),
                ).where(
                    RefundRequest.payment_order_id == order_id,
                    RefundRequest.status.in_(_ACTIVE_REFUND_STATUSES),
                )
            )
        ).one()
        return int(row[0] or 0), int(row[1] or 0)

    @staticmethod
    async def _available_balance(session: AsyncSession, user_id: UUID) -> int:
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
                    CreditReservation.user_id == user_id,
                    CreditReservation.status == "active",
                )
            )
            or 0
        )
        return max(0, ledger - reserved)

    @staticmethod
    def _refund_amount(
        order: PaymentOrder,
        units: int,
        remaining_units: int,
        committed_amount: int,
    ) -> int:
        remaining_amount = order.amount_minor - committed_amount
        if units == remaining_units:
            return remaining_amount
        return order.amount_minor * units // order.credits

    @staticmethod
    def _view(row: RefundRequest) -> RefundView:
        return RefundView(
            id=row.id,
            payment_order_id=row.payment_order_id,
            provider=row.provider,
            status=row.status,
            amount_minor=row.amount_minor,
            currency=row.currency,
            credit_units=row.credit_units,
            failure_code=row.failure_code,
            created_at=row.created_at,
        )
