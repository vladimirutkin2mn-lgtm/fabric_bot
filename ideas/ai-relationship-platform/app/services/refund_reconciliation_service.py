"""Claim-fenced provider refund creation and authoritative reconciliation."""

from datetime import UTC, datetime, timedelta
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.db.models import (
    BillingJob,
    BillingOutboxEvent,
    CreditReservation,
    CreditTransaction,
    PaymentOrder,
    RefundRequest,
    User,
)
from app.providers.payments.base import PermanentProviderError, ProviderStateMismatch
from app.providers.payments.refund_gateway import (
    AuthoritativeRefund,
    CreateRefund,
    RefundGateway,
)


class RefundReconciliationService:
    def __init__(
        self,
        sessions: async_sessionmaker[AsyncSession],
        pending_retry_seconds: int = 900,
    ) -> None:
        self._sessions = sessions
        self._pending_retry_seconds = pending_retry_seconds

    async def process_claimed(
        self,
        job_id: UUID,
        claim_id: UUID,
        refund_request_id: UUID,
        gateway: RefundGateway,
    ) -> str:
        async with self._sessions() as session:
            refund = await session.get(RefundRequest, refund_request_id)
            if refund is None:
                raise PermanentProviderError("refund_not_found")
            order = await session.get(PaymentOrder, refund.payment_order_id)
            if order is None or order.provider_payment_id is None:
                raise PermanentProviderError("refund_payment_missing")
            if refund.status in {"succeeded", "failed"}:
                return await self._complete_terminal_claim(job_id, claim_id, refund)
            if refund.status == "manual_review":
                return await self._mark_job_manual(job_id, claim_id, refund.failure_code)
            provider_refund_id = refund.provider_refund_id
            create = CreateRefund(
                user_id=refund.user_id,
                refund_request_id=refund.id,
                provider_payment_id=order.provider_payment_id,
                amount_minor=refund.amount_minor,
                currency=refund.currency,
                reason=refund.reason,
                idempotency_key=refund.idempotency_key,
            )
        fact = (
            await gateway.fetch_refund(provider_refund_id)
            if provider_refund_id
            else await gateway.create_refund(create)
        )
        return await self._apply_claimed(job_id, claim_id, refund_request_id, fact)

    async def _apply_claimed(
        self,
        job_id: UUID,
        claim_id: UUID,
        refund_request_id: UUID,
        fact: AuthoritativeRefund,
    ) -> str:
        async with self._sessions() as discovery:
            initial = await discovery.get(RefundRequest, refund_request_id)
            if initial is None:
                return "claim_lost"
            user_id = initial.user_id
            order_id = initial.payment_order_id

        async with self._sessions.begin() as session:
            user = await session.scalar(select(User).where(User.id == user_id).with_for_update())
            order = await session.scalar(
                select(PaymentOrder).where(PaymentOrder.id == order_id).with_for_update()
            )
            refund = await session.scalar(
                select(RefundRequest)
                .where(RefundRequest.id == refund_request_id)
                .with_for_update()
            )
            reservation = await session.scalar(
                select(CreditReservation)
                .where(CreditReservation.refund_request_id == refund_request_id)
                .with_for_update()
            )
            job = await session.scalar(
                select(BillingJob).where(BillingJob.id == job_id).with_for_update()
            )
            if not self._owns_claim(job, claim_id):
                return "claim_lost"
            if user is None or order is None or refund is None or reservation is None:
                return self._manual_locked(job, refund, "refund_state_missing")
            if refund.status in {"succeeded", "failed"}:
                return self._complete_locked(job, refund.status)
            mismatch = self._mismatch(order, refund, fact)
            if mismatch:
                return self._manual_locked(job, refund, mismatch)
            if refund.provider_refund_id not in {None, fact.provider_refund_id}:
                return self._manual_locked(job, refund, "refund_identity_mismatch")
            refund.provider_refund_id = fact.provider_refund_id
            refund.failure_code = fact.failure_code
            if fact.status == "pending":
                refund.status = "provider_pending"
                job.status = "pending"
                job.available_at = datetime.now(UTC) + timedelta(
                    seconds=self._pending_retry_seconds
                )
                job.last_error_code = None
                job.claimed_by = None
                job.claim_id = None
                job.lease_until = None
                return "pending"
            if fact.status == "failed":
                if reservation.status == "consumed":
                    return self._manual_locked(job, refund, "failed_refund_consumed")
                reservation.status = "released"
                refund.status = "failed"
                refund.failure_code = fact.failure_code or "provider_failed"
                session.add(
                    BillingOutboxEvent(
                        aggregate_type="refund",
                        aggregate_id=str(refund.id),
                        event_type="refund_failed",
                        payload={"failure_code": refund.failure_code},
                        idempotency_key=f"refund_failed:{refund.id}",
                    )
                )
                return self._complete_locked(job, "failed")
            if fact.status != "succeeded":
                raise ProviderStateMismatch("unknown refund status")
            if reservation.status == "released":
                return self._manual_locked(job, refund, "succeeded_refund_released")
            purchase = await session.scalar(
                select(CreditTransaction)
                .where(
                    CreditTransaction.payment_order_id == order.id,
                    CreditTransaction.type == "purchase",
                )
                .with_for_update()
            )
            if purchase is None:
                return self._manual_locked(job, refund, "purchase_transaction_missing")
            existing = await session.scalar(
                select(CreditTransaction).where(
                    CreditTransaction.refund_request_id == refund.id
                )
            )
            if existing is not None:
                if (
                    existing.amount != -refund.credit_units
                    or existing.original_purchase_transaction_id != purchase.id
                    or existing.external_payment_id != fact.provider_refund_id
                ):
                    return self._manual_locked(job, refund, "refund_ledger_mismatch")
            else:
                session.add(
                    CreditTransaction(
                        user_id=refund.user_id,
                        type="purchase_refund",
                        amount=-refund.credit_units,
                        idempotency_key=f"purchase_refund:{refund.id}",
                        payment_order_id=order.id,
                        product_code=order.product_code,
                        external_payment_id=fact.provider_refund_id,
                        external_payment_provider=refund.provider,
                        original_purchase_transaction_id=purchase.id,
                        refund_request_id=refund.id,
                    )
                )
            reservation.status = "consumed"
            refund.status = "succeeded"
            refund.failure_code = None
            session.add(
                BillingOutboxEvent(
                    aggregate_type="refund",
                    aggregate_id=str(refund.id),
                    event_type="refund_succeeded",
                    payload={
                        "credits": refund.credit_units,
                        "amount_minor": refund.amount_minor,
                        "currency": refund.currency,
                    },
                    idempotency_key=f"refund_succeeded:{refund.id}",
                )
            )
            return self._complete_locked(job, "succeeded")

    async def _complete_terminal_claim(
        self, job_id: UUID, claim_id: UUID, refund: RefundRequest
    ) -> str:
        async with self._sessions.begin() as session:
            job = await session.scalar(
                select(BillingJob).where(BillingJob.id == job_id).with_for_update()
            )
            if not self._owns_claim(job, claim_id):
                return "claim_lost"
            return self._complete_locked(job, refund.status)

    async def _mark_job_manual(
        self, job_id: UUID, claim_id: UUID, code: str | None
    ) -> str:
        async with self._sessions.begin() as session:
            job = await session.scalar(
                select(BillingJob).where(BillingJob.id == job_id).with_for_update()
            )
            if not self._owns_claim(job, claim_id):
                return "claim_lost"
            job.status = "manual_review"
            job.last_error_code = code or "refund_manual_review"
            job.claim_id = None
            job.lease_until = None
            return "manual_review"

    @staticmethod
    def _owns_claim(job: BillingJob | None, claim_id: UUID) -> bool:
        return bool(
            job is not None
            and job.status == "claimed"
            and job.claim_id == claim_id
            and job.lease_until is not None
            and job.lease_until > datetime.now(UTC)
        )

    @staticmethod
    def _mismatch(
        order: PaymentOrder,
        refund: RefundRequest,
        fact: AuthoritativeRefund,
    ) -> str | None:
        if fact.provider != refund.provider or fact.provider_payment_id != order.provider_payment_id:
            return "refund_payment_mismatch"
        if fact.amount_minor != refund.amount_minor or fact.currency != refund.currency:
            return "refund_amount_mismatch"
        if order.provider_live_mode is not None and fact.live_mode is not None:
            if order.provider_live_mode != fact.live_mode:
                return "refund_live_mode_mismatch"
        return None

    @staticmethod
    def _complete_locked(job: BillingJob, outcome: str) -> str:
        job.status = "completed"
        job.last_error_code = None
        job.claimed_by = None
        job.claim_id = None
        job.lease_until = None
        return outcome

    @staticmethod
    def _manual_locked(
        job: BillingJob,
        refund: RefundRequest | None,
        code: str,
    ) -> str:
        job.status = "manual_review"
        job.last_error_code = code
        job.claimed_by = None
        job.claim_id = None
        job.lease_until = None
        if refund is not None:
            refund.status = "manual_review"
            refund.failure_code = code
        return "manual_review"
