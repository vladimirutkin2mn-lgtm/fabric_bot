"""At-least-once analytics delivery from the transactional billing outbox."""

from datetime import UTC, datetime, timedelta
from uuid import uuid4

from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.db.models import BillingOutboxEvent
from app.providers.analytics import AnalyticsClient


class BillingOutboxWorker:
    def __init__(
        self,
        sessions: async_sessionmaker[AsyncSession],
        analytics: AnalyticsClient,
        lease_seconds: int = 60,
        retry_seconds: int = 30,
        max_attempts: int = 10,
    ) -> None:
        self._sessions, self._analytics = sessions, analytics
        self._lease, self._retry, self._max = lease_seconds, retry_seconds, max_attempts

    async def run_once(self, worker_id: str) -> bool:
        now = datetime.now(UTC)
        async with self._sessions.begin() as session:
            event = await session.scalar(
                select(BillingOutboxEvent)
                .where(
                    BillingOutboxEvent.available_at <= now,
                    or_(
                        BillingOutboxEvent.status == "pending",
                        (BillingOutboxEvent.status == "claimed")
                        & (BillingOutboxEvent.lease_until < now),
                    ),
                )
                .with_for_update(skip_locked=True)
                .limit(1)
            )
            if not event:
                return False
            event.status, event.claimed_by, event.claimed_at = "claimed", worker_id, now
            claim_id = uuid4()
            event.claim_id = claim_id
            event.lease_until, event.attempt_count = (
                now + timedelta(seconds=self._lease),
                event.attempt_count + 1,
            )
            event_id, aggregate, kind, payload = (
                event.id,
                event.aggregate_id,
                event.event_type,
                event.payload,
            )
            idempotency_key = event.idempotency_key
        try:
            properties = {k: str(v) for k, v in payload.items()}
            properties["outbox_idempotency_key"] = idempotency_key
            properties["outbox_event_id"] = str(event_id)
            await self._analytics.track(aggregate, kind, properties)
        except Exception:
            async with self._sessions.begin() as session:
                event = await session.get(BillingOutboxEvent, event_id, with_for_update=True)
                if event and event.claim_id == claim_id:
                    event.status = "failed" if event.attempt_count >= self._max else "pending"
                    event.last_error_code, event.claimed_by, event.lease_until = (
                        "delivery_failed",
                        None,
                        None,
                    )
                    event.available_at = datetime.now(UTC) + timedelta(seconds=self._retry)
                    event.claim_id = None
            return True
        async with self._sessions.begin() as session:
            event = await session.get(BillingOutboxEvent, event_id, with_for_update=True)
            if event and event.claim_id == claim_id:
                event.status, event.completed_at, event.lease_until = (
                    "completed",
                    datetime.now(UTC),
                    None,
                )
                event.claim_id = None
        return True
