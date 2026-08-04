"""Periodic stale-order sweeper; it only enqueues durable reconciliation work."""

from datetime import UTC, datetime, timedelta

from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.db.models import BillingJob, PaymentOrder


class PaymentReconciliationSweeper:
    def __init__(
        self,
        sessions: async_sessionmaker[AsyncSession],
        stale_seconds: int,
        supported_providers: set[str] | None = None,
    ) -> None:
        self._sessions = sessions
        self._stale = stale_seconds
        self._supported = (
            {"stripe", "yookassa"} if supported_providers is None else supported_providers
        )

    def supports_provider(self, provider: str) -> bool:
        return provider in self._supported

    async def enqueue_stale(self, limit: int = 100) -> int:
        now = datetime.now(UTC)
        cutoff = now - timedelta(seconds=self._stale)
        count = 0
        async with self._sessions.begin() as session:
            orders = list(
                await session.scalars(
                    select(PaymentOrder)
                    .where(
                        PaymentOrder.status.in_(("creating", "pending")),
                        PaymentOrder.provider.in_(self._supported),
                        PaymentOrder.updated_at <= cutoff,
                        or_(
                            PaymentOrder.last_reconciled_at.is_(None),
                            PaymentOrder.last_reconciled_at <= cutoff,
                        ),
                    )
                    .order_by(PaymentOrder.updated_at)
                    .with_for_update(skip_locked=True)
                    .limit(limit)
                )
            )
            for order in orders:
                key = f"reconcile:{order.id}"
                job = await session.scalar(
                    select(BillingJob).where(BillingJob.idempotency_key == key).with_for_update()
                )
                if job is None:
                    session.add(
                        BillingJob(
                            job_type="payment_reconciliation",
                            provider=order.provider,
                            object_type="payment_order",
                            object_id=str(order.id),
                            idempotency_key=key,
                        )
                    )
                elif job.status in {"completed", "failed"}:
                    job.status = "pending"
                    job.available_at = now
                    job.claim_id = None
                    job.claimed_by = None
                    job.lease_until = None
                else:
                    continue
                order.last_reconciled_at = now
                count += 1
        return count
