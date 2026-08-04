"""Real PostgreSQL outbox stale-claim protection."""

import asyncio
from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.db.models import BillingOutboxEvent
from app.services.billing_outbox_service import BillingOutboxWorker

pytestmark = pytest.mark.postgres


class BlockingAnalytics:
    def __init__(self) -> None:
        self.started = asyncio.Event()
        self.release = asyncio.Event()

    async def track(self, user_id, event, properties=None):  # type: ignore[no-untyped-def]
        self.started.set()
        await self.release.wait()


class RecordingAnalytics:
    def __init__(self) -> None:
        self.calls = 0

    async def track(self, user_id, event, properties=None):  # type: ignore[no-untyped-def]
        self.calls += 1


async def test_stale_outbox_delivery_cannot_overwrite_reclaimed_completion(
    payment_db: async_sessionmaker[AsyncSession],
) -> None:
    async with payment_db.begin() as session:
        event = BillingOutboxEvent(
            aggregate_type="payment_order",
            aggregate_id=str(uuid4()),
            event_type="purchase_completed",
            payload={},
            idempotency_key=f"outbox:{uuid4()}",
        )
        session.add(event)
        await session.flush()
        event_id = event.id
    blocked = BlockingAnalytics()
    task = asyncio.create_task(
        BillingOutboxWorker(payment_db, blocked, lease_seconds=1).run_once("a")
    )
    await blocked.started.wait()
    async with payment_db.begin() as session:
        claimed = await session.get(BillingOutboxEvent, event_id)
        assert claimed is not None
        claimed.lease_until = datetime.now(UTC) - timedelta(seconds=1)
    recorder = RecordingAnalytics()
    assert await BillingOutboxWorker(payment_db, recorder).run_once("b")
    blocked.release.set()
    await task
    async with payment_db() as session:
        persisted = await session.get(BillingOutboxEvent, event_id)
    assert recorder.calls == 1
    assert persisted is not None and persisted.status == "completed"
