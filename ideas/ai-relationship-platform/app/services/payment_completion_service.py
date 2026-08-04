"""Exactly-once completion from normalized authoritative provider state."""

from datetime import UTC, datetime
from typing import cast
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert
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


def postgres_constraint_name(error: BaseException) -> str | None:
    """Extract an exact driver-provided constraint identifier without message parsing."""
    pending: list[BaseException] = [error]
    seen: set[int] = set()
    while pending:
        current = pending.pop()
        if id(current) in seen:
            continue
        seen.add(id(current))
        direct = getattr(current, "constraint_name", None)
        if isinstance(direct, str) and direct.isidentifier():
            return direct
        diag = getattr(current, "diag", None)
        diagnosed = getattr(diag, "constraint_name", None)
        if isinstance(diagnosed, str) and diagnosed.isidentifier():
            return diagnosed
        for nested in (
            getattr(current, "orig", None),
            current.__cause__,
            current.__context__,
        ):
            if isinstance(nested, BaseException):
                pending.append(nested)
    return None


class PaymentCompletionService:
    def __init__(
        self, sessions: async_sessionmaker[AsyncSession], production: bool = False
    ) -> None:
        self._sessions, self._production = sessions, production

    async def complete(self, order_id: UUID, payment: AuthoritativePayment) -> str:
        try:
            return await self._complete(order_id, payment)
        except IntegrityError as exc:
            constraint = postgres_constraint_name(exc)
            if constraint not in self._known_identity_constraints():
                raise
            return await self._resolve_identity_conflict(order_id, payment.payment_id)

    async def _resolve_identity_conflict(self, order_id: UUID, payment_id: str) -> str:
        async with self._sessions.begin() as session:
            initial = await session.get(PaymentOrder, order_id)
            if initial is None:
                return "order_not_found"
            await session.scalar(select(User).where(User.id == initial.user_id).with_for_update())
            order = await self._lock_order(session, order_id)
            assert order is not None
            if order.status == "completed" and order.provider_payment_id == payment_id:
                return "already_completed"
            owner = await session.scalar(
                select(PaymentOrder.id).where(
                    PaymentOrder.provider_payment_id == payment_id,
                    PaymentOrder.provider == order.provider,
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
                # Discovery reads are non-authoritative. Mutation locks always use the global
                # User -> PaymentOrder -> BillingJob -> ProviderWebhookEvent order.
                discovered_job = await session.get(BillingJob, job_id)
                initial = await session.get(PaymentOrder, order_id)
                if discovered_job is None or initial is None:
                    return "claim_lost"
                event_id = (
                    UUID(discovered_job.object_id)
                    if discovered_job.job_type == "webhook_processing"
                    else None
                )
                user = await session.scalar(
                    select(User).where(User.id == initial.user_id).with_for_update()
                )
                order = await self._lock_order(session, order_id)
                if order is None:
                    return "claim_lost"
                job = await session.scalar(
                    select(BillingJob)
                    .where(BillingJob.id == job_id)
                    .with_for_update()
                    .execution_options(populate_existing=True)
                )
                if (
                    job is None
                    or job.claim_id != claim_id
                    or job.status != "claimed"
                    or job.lease_until is None
                    or job.lease_until <= datetime.now(UTC)
                ):
                    return "claim_lost"
                event: ProviderWebhookEvent | None = None
                if event_id is not None:
                    event = await session.get(ProviderWebhookEvent, event_id, with_for_update=True)
                    if (
                        event is None
                        or event.status == "manual_review"
                        or event.last_error_code == "duplicate_payload_mismatch"
                    ):
                        self._manual_review_locked(job, event, order, "duplicate_payload_mismatch")
                        return "manual_review"
                if user is None or user.privacy_status != "active":
                    order.status, order.failure_code = "cancelled", "user_deleted"
                    order.encrypted_receipt_contact = None
                    job.status, job.last_error_code = "manual_review", "user_deleted"
                    job.claim_id, job.lease_until = None, None
                    if event is not None:
                        event.status, event.last_error_code = "manual_review", "user_deleted"
                    return "user_deleted"
                outcome = await self._apply_locked(session, order, payment)
                manual = self._is_manual_review_outcome(outcome)
                job_error = order.failure_code
                if order.status == "completed" and payment.payment_id != order.provider_payment_id:
                    job_error = "completed_payment_identity_mismatch"
                job.status = "manual_review" if manual else "completed"
                job.last_error_code = job_error if manual else None
                job.claim_id, job.lease_until = None, None
                if event is not None:
                    event.status = "manual_review" if manual else "completed"
                    event.last_error_code = job_error if manual else None
                    event.processed_at = datetime.now(UTC)
                return outcome
        except IntegrityError as exc:
            constraint = postgres_constraint_name(exc)
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
            user = await session.scalar(
                select(User).where(User.id == initial.user_id).with_for_update()
            )
            order = await self._lock_order(session, order_id)
            assert order is not None
            if user is None or user.privacy_status != "active":
                order.status, order.failure_code = "cancelled", "user_deleted"
                order.encrypted_receipt_contact = None
                return "user_deleted"
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
                await self._outbox(session, order, "payment_failed")
                return "failed"
            order.provider_status = payment.provider_status or payment.status
            return "pending"
        owner = await session.scalar(
            select(PaymentOrder.id).where(
                PaymentOrder.provider_payment_id == payment.payment_id,
                PaymentOrder.provider == order.provider,
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
                external_payment_provider=order.provider,
            )
        )
        await self._outbox(session, order, "purchase_completed")
        return "completed"

    @staticmethod
    def _manual_review_locked(
        job: BillingJob,
        event: ProviderWebhookEvent | None,
        order: PaymentOrder,
        code: str,
    ) -> None:
        """Mutate rows already locked in canonical user/order/job/event order."""
        if order.status in {"creating", "pending"}:
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
            discovered_job = await session.get(BillingJob, job_id)
            initial = await session.get(PaymentOrder, order_id)
            if discovered_job is None or initial is None:
                return "claim_lost"
            event_id = (
                UUID(discovered_job.object_id)
                if discovered_job.job_type == "webhook_processing"
                else None
            )
            user = await session.scalar(
                select(User).where(User.id == initial.user_id).with_for_update()
            )
            order = await self._lock_order(session, order_id)
            if order is None:
                return "claim_lost"
            job = await session.scalar(
                select(BillingJob)
                .where(BillingJob.id == job_id)
                .with_for_update()
                .execution_options(populate_existing=True)
            )
            if (
                job is None
                or job.claim_id != claim_id
                or job.status != "claimed"
                or job.lease_until is None
                or job.lease_until <= datetime.now(UTC)
            ):
                return "claim_lost"
            event = (
                await session.get(ProviderWebhookEvent, event_id, with_for_update=True)
                if event_id is not None
                else None
            )
            if user is None or user.privacy_status != "active":
                order.status, order.failure_code = "cancelled", "user_deleted"
                job.status, job.last_error_code = "manual_review", "user_deleted"
                if event is not None:
                    event.status, event.last_error_code = "manual_review", "user_deleted"
                job.claim_id, job.lease_until = None, None
                return "user_deleted"
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
            "uq_payment_provider_payment",
            "uq_credit_external_payment_provider_id",
            "credit_transactions_payment_order_id_key",
            "credit_transactions_idempotency_key_key",
            "billing_outbox_events_idempotency_key_key",
        }

    @staticmethod
    def _is_manual_review_outcome(outcome: str) -> bool:
        return outcome in {
            "manual_review",
            "identity_conflict",
            "already_manual_review",
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
    async def _lock_order(session: AsyncSession, order_id: UUID) -> PaymentOrder | None:
        return cast(
            PaymentOrder | None,
            await session.scalar(
                select(PaymentOrder)
                .where(PaymentOrder.id == order_id)
                .with_for_update()
                .execution_options(populate_existing=True)
            ),
        )

    @staticmethod
    async def _outbox(session: AsyncSession, order: PaymentOrder, event: str) -> None:
        payload = {
            "product_code": order.product_code,
            "provider": order.provider,
            "credits": order.credits,
        }
        key = f"{event}:{order.id}"
        inserted = await session.scalar(
            insert(BillingOutboxEvent)
            .values(
                aggregate_type="payment_order",
                aggregate_id=str(order.id),
                event_type=event,
                payload=payload,
                idempotency_key=key,
                status="pending",
                attempt_count=0,
            )
            .on_conflict_do_nothing(index_elements=["idempotency_key"])
            .returning(BillingOutboxEvent.id)
        )
        if inserted is None:
            existing = await session.scalar(
                select(BillingOutboxEvent).where(BillingOutboxEvent.idempotency_key == key)
            )
            if (
                existing is None
                or existing.aggregate_type != "payment_order"
                or existing.aggregate_id != str(order.id)
                or existing.event_type != event
                or existing.payload != payload
            ):
                raise RuntimeError("billing outbox idempotency mismatch")
