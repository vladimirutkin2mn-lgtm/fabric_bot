"""Exactly-once completion from normalized authoritative provider state."""

from datetime import UTC, datetime
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.db.models import (
    BillingJob,
    BillingOutboxEvent,
    CreditTransaction,
    PaymentOrder,
    ProviderWebhookEvent,
    User,
)
from app.providers.payments.gateway import AuthoritativePayment


class PaymentCompletionService:
    def __init__(
        self, sessions: async_sessionmaker[AsyncSession], production: bool = False
    ) -> None:
        self._sessions, self._production = sessions, production

    async def complete(self, order_id: UUID, payment: AuthoritativePayment) -> str:
        try:
            return await self._complete(order_id, payment)
        except IntegrityError as exc:
            constraint = getattr(getattr(exc.orig, "diag", None), "constraint_name", None)
            if constraint not in self._known_identity_constraints():
                raise
            return await self._resolve_identity_conflict(order_id, payment.payment_id)

    async def _resolve_identity_conflict(self, order_id: UUID, payment_id: str) -> str:
        async with self._sessions.begin() as session:
            initial = await session.get(PaymentOrder, order_id)
            if initial is None:
                return "order_not_found"
            await session.scalar(select(User).where(User.id == initial.user_id).with_for_update())
            order = await session.get(PaymentOrder, order_id, with_for_update=True)
            assert order is not None
            if order.status == "completed" and order.provider_payment_id == payment_id:
                return "already_completed"
            owner = await session.scalar(
                select(PaymentOrder.id).where(
                    PaymentOrder.provider_payment_id == payment_id,
                    PaymentOrder.id != order.id,
                )
            )
            if owner is not None:
                order.status = "manual_review"
                order.failure_code = "payment_identity_reused"
                order.encrypted_receipt_contact = None
                return "manual_review"
            order.status = "manual_review"
            order.failure_code = "payment_identity_conflict"
            order.encrypted_receipt_contact = None
            return "manual_review"

    async def complete_claimed(
        self,
        job_id: UUID,
        claim_id: UUID,
        order_id: UUID,
        payment: AuthoritativePayment,
    ) -> str:
        """Apply financial state only while the caller still owns the durable claim."""
        try:
            async with self._sessions.begin() as session:
                job = await session.get(BillingJob, job_id, with_for_update=True)
                if (
                    job is None
                    or job.claim_id != claim_id
                    or job.status != "claimed"
                    or job.lease_until is None
                    or job.lease_until <= datetime.now(UTC)
                ):
                    return "claim_lost"
                event: ProviderWebhookEvent | None = None
                if job.job_type == "webhook_processing":
                    event = await session.get(
                        ProviderWebhookEvent, UUID(job.object_id), with_for_update=True
                    )
                    if (
                        event is None
                        or event.status == "manual_review"
                        or event.last_error_code == "duplicate_payload_mismatch"
                    ):
                        await self._manual_review_locked(
                            session, job, event, order_id, "duplicate_payload_mismatch"
                        )
                        return "manual_review"
                initial = await session.get(PaymentOrder, order_id)
                if initial is None:
                    job.status, job.claim_id = "manual_review", None
                    job.last_error_code = "order_not_found"
                    return "manual_review"
                await session.scalar(
                    select(User).where(User.id == initial.user_id).with_for_update()
                )
                order = await session.get(PaymentOrder, order_id, with_for_update=True)
                assert order is not None
                outcome = await self._apply_locked(session, order, payment)
                manual = outcome in {"manual_review", "identity_conflict"}
                job.status = "manual_review" if manual else "completed"
                job.last_error_code = order.failure_code if manual else None
                job.claim_id, job.lease_until = None, None
                if event is not None:
                    event.status = "manual_review" if manual else "completed"
                    event.last_error_code = order.failure_code if manual else None
                    event.processed_at = datetime.now(UTC)
                return outcome
        except IntegrityError as exc:
            constraint = getattr(getattr(exc.orig, "diag", None), "constraint_name", None)
            if constraint not in self._known_identity_constraints():
                raise
            return await self._resolve_claimed_identity_conflict(
                job_id, claim_id, order_id, payment.payment_id
            )

    async def _complete(self, order_id: UUID, payment: AuthoritativePayment) -> str:
        async with self._sessions.begin() as session:
            initial = await session.get(PaymentOrder, order_id)
            if initial is None:
                return "order_not_found"
            await session.scalar(select(User).where(User.id == initial.user_id).with_for_update())
            order = await session.get(PaymentOrder, order_id, with_for_update=True)
            assert order is not None
            return await self._apply_locked(session, order, payment)

    async def _apply_locked(
        self, session: AsyncSession, order: PaymentOrder, payment: AuthoritativePayment
    ) -> str:
        if order.status == "completed":
            return (
                "already_completed"
                if order.provider_payment_id == payment.payment_id
                else "manual_review"
            )
        if order.status in {"failed", "cancelled", "manual_review"}:
            return f"already_{order.status}"
        mismatch = self._mismatch(order, payment)
        if mismatch:
            order.status, order.failure_code = "manual_review", mismatch
            order.encrypted_receipt_contact = None
            return "manual_review"
        if not payment.paid or payment.status != "succeeded":
            if payment.status == "waiting_for_capture":
                order.status, order.failure_code = "manual_review", "unexpected_waiting_for_capture"
                order.encrypted_receipt_contact = None
                return "manual_review"
            if payment.status in {"failed", "canceled", "cancelled", "expired"}:
                order.status, order.failure_code = "failed", f"provider_{payment.status}"
                order.encrypted_receipt_contact = None
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
        if owner is not None:
            order.status, order.failure_code = "manual_review", "payment_identity_reused"
            order.encrypted_receipt_contact = None
            return "manual_review"
        order.status, order.completed_at = "completed", datetime.now(UTC)
        order.encrypted_receipt_contact = None
        order.provider_payment_id = payment.payment_id
        order.provider_status = payment.provider_status or payment.status
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

    async def _manual_review_locked(
        self,
        session: AsyncSession,
        job: BillingJob,
        event: ProviderWebhookEvent | None,
        order_id: UUID,
        code: str,
    ) -> None:
        initial = await session.get(PaymentOrder, order_id)
        if initial is not None:
            await session.scalar(select(User).where(User.id == initial.user_id).with_for_update())
        order = await session.get(PaymentOrder, order_id, with_for_update=True)
        if order is not None and order.status in {"creating", "pending"}:
            order.status, order.failure_code = "manual_review", code
            order.encrypted_receipt_contact = None
        job.status, job.last_error_code, job.claim_id = "manual_review", code, None
        job.lease_until = None
        if event is not None:
            event.status, event.last_error_code = "manual_review", code

    async def _resolve_claimed_identity_conflict(
        self, job_id: UUID, claim_id: UUID, order_id: UUID, payment_id: str
    ) -> str:
        async with self._sessions.begin() as session:
            job = await session.get(BillingJob, job_id, with_for_update=True)
            if (
                job is None
                or job.claim_id != claim_id
                or job.lease_until is None
                or job.lease_until <= datetime.now(UTC)
            ):
                return "claim_lost"
            event = None
            if job.job_type == "webhook_processing":
                event = await session.get(
                    ProviderWebhookEvent, UUID(job.object_id), with_for_update=True
                )
            initial = await session.get(PaymentOrder, order_id)
            if initial is None:
                job.status, job.claim_id = "manual_review", None
                return "manual_review"
            await session.scalar(select(User).where(User.id == initial.user_id).with_for_update())
            order = await session.get(PaymentOrder, order_id, with_for_update=True)
            assert order is not None
            if order.status == "completed" and order.provider_payment_id == payment_id:
                outcome = "already_completed"
                job.status = "completed"
                if event is not None:
                    event.status = "completed"
            else:
                outcome = "manual_review"
                order.status, order.failure_code = "manual_review", "payment_identity_reused"
                order.encrypted_receipt_contact = None
                job.status, job.last_error_code = "manual_review", "payment_identity_reused"
                if event is not None:
                    event.status = "manual_review"
                    event.last_error_code = "payment_identity_reused"
            job.claim_id, job.lease_until = None, None
            return outcome

    @staticmethod
    def _known_identity_constraints() -> set[str]:
        return {
            "payment_orders_provider_payment_id_key",
            "credit_transactions_external_payment_id_key",
            "credit_transactions_payment_order_id_key",
            "credit_transactions_idempotency_key_key",
            "billing_outbox_events_idempotency_key_key",
        }

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
