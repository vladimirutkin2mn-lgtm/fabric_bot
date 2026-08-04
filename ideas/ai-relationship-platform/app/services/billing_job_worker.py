"""Lease-based PostgreSQL billing job worker."""

import asyncio
import logging
from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.db.models import BillingJob, PaymentOrder, ProviderWebhookEvent
from app.providers.payments.base import PermanentProviderError, UnknownProviderOutcome
from app.providers.payments.gateway import CreateCheckout, OneTimePaymentGateway
from app.services.checkout_service import CheckoutRejected, ReceiptContactCipher
from app.services.payment_completion_service import PaymentCompletionService

logger = logging.getLogger(__name__)


class BillingJobWorker:
    def __init__(
        self,
        sessions: async_sessionmaker[AsyncSession],
        gateways: dict[str, OneTimePaymentGateway],
        completion: PaymentCompletionService,
        lease_seconds: int = 60,
        retry_base_seconds: int = 30,
        max_attempts: int = 10,
        public_base_url: str = "http://localhost:8000",
        receipt_cipher: ReceiptContactCipher | None = None,
    ) -> None:
        self._sessions, self._gateways, self._completion = sessions, gateways, completion
        self._lease, self._base, self._max = lease_seconds, retry_base_seconds, max_attempts
        self._public_base_url = public_base_url.rstrip("/")
        self._receipt_cipher = receipt_cipher

    async def claim_one(self, worker_id: str) -> tuple[UUID, UUID] | None:
        now = datetime.now(UTC)
        async with self._sessions.begin() as session:
            job = await session.scalar(
                select(BillingJob)
                .where(
                    BillingJob.available_at <= now,
                    or_(
                        BillingJob.status == "pending",
                        (BillingJob.status == "claimed") & (BillingJob.lease_until < now),
                    ),
                )
                .order_by(BillingJob.available_at, BillingJob.created_at)
                .with_for_update(skip_locked=True)
                .limit(1)
            )
            if not job:
                return None
            claim_id = uuid4()
            job.status, job.claimed_by, job.claimed_at = "claimed", worker_id, now
            job.claim_id = claim_id
            job.lease_until, job.attempt_count = (
                now + timedelta(seconds=self._lease),
                job.attempt_count + 1,
            )
            return job.id, claim_id

    async def run_once(self, worker_id: str) -> bool:
        claim = await self.claim_one(worker_id)
        if not claim:
            return False
        job_id, claim_id = claim
        try:
            await self._process(job_id, claim_id)
        except UnknownProviderOutcome:
            await self._retry(job_id, claim_id, "provider_unknown")
        except PermanentProviderError as exc:
            code = str(exc)
            if code not in {"unsupported_provider", "corrupt_receipt_contact"}:
                code = "provider_validation"
            await self._manual(job_id, claim_id, code)
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("billing_job_unexpected job_id=%s", job_id)
            await self._retry(job_id, claim_id, "unexpected_job_error")
        return True

    async def _process(self, job_id: UUID, claim_id: UUID) -> None:
        async with self._sessions() as session:
            job = await session.get(BillingJob, job_id)
            if not job:
                return
            if job.job_type == "webhook_processing":
                event = await session.get(ProviderWebhookEvent, UUID(job.object_id))
                if not event:
                    raise PermanentProviderError()
                if event.status == "manual_review":
                    raise PermanentProviderError("webhook_manual_review")
                checkout_id = event.provider_object_id
                order = await session.scalar(
                    select(PaymentOrder).where(
                        PaymentOrder.provider == event.provider,
                        (PaymentOrder.provider_checkout_id == checkout_id)
                        | (PaymentOrder.provider_payment_id == checkout_id),
                    )
                )
            else:
                order = await session.get(PaymentOrder, UUID(job.object_id))
            if not order:
                raise PermanentProviderError()
            provider, checkout = order.provider, order.provider_checkout_id
            if provider not in self._gateways:
                raise PermanentProviderError("unsupported_provider")
            order_id = order.id
        if not checkout:
            checkout = await self._retry_checkout_creation(order_id)
        payment = await self._gateways[provider].fetch_payment(checkout)
        await self._completion.complete_claimed(job_id, claim_id, order_id, payment)

    async def _retry_checkout_creation(self, order_id: UUID) -> str:
        async with self._sessions() as session:
            order = await session.get(PaymentOrder, order_id)
            if not order:
                raise PermanentProviderError()
            snapshot = order.commercial_snapshot
            contact = None
            if order.encrypted_receipt_contact:
                if self._receipt_cipher is None:
                    raise PermanentProviderError("receipt_cipher_missing")
                try:
                    contact = self._receipt_cipher.decrypt(order.encrypted_receipt_contact)
                except CheckoutRejected as exc:
                    raise PermanentProviderError("corrupt_receipt_contact") from exc
            request = CreateCheckout(
                str(order.id),
                order.product_code,
                order.product_version,
                order.amount_minor,
                order.currency,
                str(snapshot.get("price_reference", "")),
                order.idempotency_key or "",
                f"{self._public_base_url}/payments/return/{order.checkout_token}",
                f"{self._public_base_url}/payments/return/{order.checkout_token}",
                contact,
            )
            provider = order.provider
        hosted = await self._gateways[provider].create_checkout(request)
        async with self._sessions.begin() as session:
            order = await session.get(PaymentOrder, order_id, with_for_update=True)
            if not order:
                raise PermanentProviderError()
            if order.provider_checkout_id and order.provider_checkout_id != hosted.checkout_id:
                order.status, order.failure_code = "manual_review", "checkout_identity_mismatch"
                order.encrypted_receipt_contact = None
                raise PermanentProviderError("checkout_identity_mismatch")
            order.provider_checkout_id = hosted.checkout_id
            order.checkout_url = hosted.url
            order.provider_status = hosted.status
            order.checkout_expires_at = hosted.expires_at
            order.provider_live_mode = hosted.live_mode
            order.status = "pending"
            order.encrypted_receipt_contact = None
        return hosted.checkout_id

    async def _retry(self, job_id: UUID, claim_id: UUID, code: str) -> None:
        async with self._sessions.begin() as session:
            job = await session.get(BillingJob, job_id, with_for_update=True)
            if not job or job.claim_id != claim_id:
                return
            job.last_error_code, job.claimed_by, job.lease_until = code, None, None
            job.claim_id = None
            if job.attempt_count >= self._max:
                job.status = "manual_review"
                job.last_error_code = "retry_exhausted"
                await self._mark_related_manual(session, job, "retry_exhausted")
            else:
                job.status = "pending"
                job.available_at = datetime.now(UTC) + timedelta(
                    seconds=min(self._base * 2 ** (job.attempt_count - 1), 3600)
                )

    async def _manual(self, job_id: UUID, claim_id: UUID, code: str) -> None:
        async with self._sessions.begin() as session:
            job = await session.get(BillingJob, job_id, with_for_update=True)
            if job and job.claim_id == claim_id:
                job.status, job.last_error_code, job.lease_until = "manual_review", code, None
                job.claim_id = None
                await self._mark_related_manual(session, job, code)

    @staticmethod
    async def _mark_related_manual(session: AsyncSession, job: BillingJob, code: str) -> None:
        order: PaymentOrder | None = None
        if job.job_type == "payment_reconciliation":
            order = await session.get(PaymentOrder, UUID(job.object_id), with_for_update=True)
        elif job.job_type == "webhook_processing":
            event = await session.get(
                ProviderWebhookEvent, UUID(job.object_id), with_for_update=True
            )
            if event is not None:
                event.status = "manual_review"
                event.last_error_code = code
                order = await session.scalar(
                    select(PaymentOrder)
                    .where(
                        PaymentOrder.provider == event.provider,
                        (PaymentOrder.provider_checkout_id == event.provider_object_id)
                        | (PaymentOrder.provider_payment_id == event.provider_object_id),
                    )
                    .with_for_update()
                )
        if order is not None and order.status in {"creating", "pending"}:
            order.status = "manual_review"
            order.failure_code = code
            order.encrypted_receipt_contact = None
