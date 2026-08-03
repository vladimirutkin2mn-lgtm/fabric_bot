"""Lease-based PostgreSQL billing job worker."""

from datetime import UTC, datetime, timedelta
from uuid import UUID

from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.db.models import BillingJob, PaymentOrder, ProviderWebhookEvent
from app.providers.payments.base import PermanentProviderError, UnknownProviderOutcome
from app.providers.payments.gateway import OneTimePaymentGateway
from app.services.payment_completion_service import PaymentCompletionService


class BillingJobWorker:
    def __init__(
        self,
        sessions: async_sessionmaker[AsyncSession],
        gateways: dict[str, OneTimePaymentGateway],
        completion: PaymentCompletionService,
        lease_seconds: int = 60,
        retry_base_seconds: int = 30,
        max_attempts: int = 10,
    ) -> None:
        self._sessions, self._gateways, self._completion = sessions, gateways, completion
        self._lease, self._base, self._max = lease_seconds, retry_base_seconds, max_attempts

    async def claim_one(self, worker_id: str) -> UUID | None:
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
            job.status, job.claimed_by, job.claimed_at = "claimed", worker_id, now
            job.lease_until, job.attempt_count = (
                now + timedelta(seconds=self._lease),
                job.attempt_count + 1,
            )
            return job.id

    async def run_once(self, worker_id: str) -> bool:
        job_id = await self.claim_one(worker_id)
        if not job_id:
            return False
        try:
            await self._process(job_id)
        except UnknownProviderOutcome:
            await self._retry(job_id, "provider_unknown")
        except PermanentProviderError:
            await self._manual(job_id, "provider_validation")
        return True

    async def _process(self, job_id: UUID) -> None:
        async with self._sessions() as session:
            job = await session.get(BillingJob, job_id)
            if not job:
                return
            if job.job_type == "webhook_processing":
                event = await session.get(ProviderWebhookEvent, UUID(job.object_id))
                if not event:
                    raise PermanentProviderError()
                checkout_id = event.provider_object_id
                order = await session.scalar(
                    select(PaymentOrder).where(
                        (PaymentOrder.provider_checkout_id == checkout_id)
                        | (PaymentOrder.provider_payment_id == checkout_id)
                    )
                )
            else:
                order = await session.get(PaymentOrder, UUID(job.object_id))
            if not order:
                raise PermanentProviderError()
            provider, checkout = order.provider, order.provider_checkout_id
            if not checkout:
                raise UnknownProviderOutcome()
        payment = await self._gateways[provider].fetch_payment(checkout)
        outcome = await self._completion.complete(order.id, payment)
        async with self._sessions.begin() as session:
            current = await session.get(BillingJob, job_id, with_for_update=True)
            if current:
                current.status = (
                    "completed" if outcome not in {"manual_review"} else "manual_review"
                )
                current.lease_until = None
            if job.job_type == "webhook_processing":
                event = await session.get(
                    ProviderWebhookEvent, UUID(job.object_id), with_for_update=True
                )
                if event:
                    event.status = "completed" if outcome != "manual_review" else "manual_review"
                    event.processed_at = datetime.now(UTC)

    async def _retry(self, job_id: UUID, code: str) -> None:
        async with self._sessions.begin() as session:
            job = await session.get(BillingJob, job_id, with_for_update=True)
            if not job:
                return
            job.last_error_code, job.claimed_by, job.lease_until = code, None, None
            if job.attempt_count >= self._max:
                job.status = "manual_review"
            else:
                job.status = "pending"
                job.available_at = datetime.now(UTC) + timedelta(
                    seconds=min(self._base * 2 ** (job.attempt_count - 1), 3600)
                )

    async def _manual(self, job_id: UUID, code: str) -> None:
        async with self._sessions.begin() as session:
            job = await session.get(BillingJob, job_id, with_for_update=True)
            if job:
                job.status, job.last_error_code, job.lease_until = "manual_review", code, None
