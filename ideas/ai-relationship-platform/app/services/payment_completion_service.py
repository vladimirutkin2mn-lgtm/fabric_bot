"""Exactly-once completion from normalized authoritative provider state."""

from datetime import UTC, datetime
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.db.models import BillingOutboxEvent, CreditTransaction, PaymentOrder, User
from app.providers.payments.gateway import AuthoritativePayment


class PaymentCompletionService:
    def __init__(
        self, sessions: async_sessionmaker[AsyncSession], production: bool = False
    ) -> None:
        self._sessions, self._production = sessions, production

    async def complete(self, order_id: UUID, payment: AuthoritativePayment) -> str:
        async with self._sessions.begin() as session:
            order0 = await session.get(PaymentOrder, order_id)
            if not order0:
                return "order_not_found"
            await session.scalar(select(User).where(User.id == order0.user_id).with_for_update())
            order = await session.get(PaymentOrder, order_id, with_for_update=True)
            assert order
            if order.status == "completed":
                return (
                    "already_completed"
                    if order.provider_payment_id == payment.payment_id
                    else "manual_review"
                )
            mismatch = self._mismatch(order, payment)
            if mismatch:
                order.status, order.failure_code = "manual_review", mismatch
                return "manual_review"
            if not payment.paid or payment.status != "succeeded":
                if payment.status in {"failed", "canceled", "cancelled", "expired"}:
                    order.status, order.failure_code = "failed", f"provider_{payment.status}"
                    self._outbox(session, order, "payment_failed")
                    return "failed"
                order.provider_status = payment.provider_status or payment.status
                return "pending"
            owner = await session.scalar(
                select(PaymentOrder.id).where(
                    PaymentOrder.provider_payment_id == payment.payment_id,
                    PaymentOrder.id != order.id,
                )
            )
            if owner:
                order.status, order.failure_code = "manual_review", "payment_identity_reused"
                return "manual_review"
            order.status, order.completed_at = "completed", datetime.now(UTC)
            order.provider_payment_id, order.provider_status = (
                payment.payment_id,
                payment.provider_status or payment.status,
            )
            session.add(
                CreditTransaction(
                    user_id=order.user_id,
                    type="purchase",
                    amount=order.credits,
                    idempotency_key=f"purchase:{order.id}",
                    payment_order_id=order.id,
                    product_code=order.product_code,
                    external_payment_id=payment.payment_id,
                )
            )
            self._outbox(session, order, "purchase_completed")
            return "completed"

    def _mismatch(self, order: PaymentOrder, payment: AuthoritativePayment) -> str | None:
        if payment.checkout_id != order.provider_checkout_id:
            return "checkout_mismatch"
        if payment.order_id != str(order.id):
            return "metadata_mismatch"
        if payment.amount_minor != order.amount_minor:
            return "amount_mismatch"
        if payment.currency.upper() != order.currency:
            return "currency_mismatch"
        if order.provider == "stripe" and payment.mode != "payment":
            return "mode_mismatch"
        if self._production and payment.live_mode is not True:
            return "live_mode_mismatch"
        return None

    @staticmethod
    def _outbox(session: AsyncSession, order: PaymentOrder, event: str) -> None:
        session.add(
            BillingOutboxEvent(
                aggregate_type="payment_order",
                aggregate_id=str(order.id),
                event_type=event,
                payload={
                    "product_code": order.product_code,
                    "provider": order.provider,
                    "credits": order.credits,
                },
                idempotency_key=f"{event}:{order.id}",
            )
        )
