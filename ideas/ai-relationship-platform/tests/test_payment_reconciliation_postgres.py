"""Real PostgreSQL stale-order reconciliation selection."""

from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.db.models import BillingJob, PaymentOrder
from app.services.payment_reconciliation_service import PaymentReconciliationSweeper
from tests.payment_postgres_helpers import create_order

pytestmark = pytest.mark.postgres
pytest_plugins = ("tests.payment_postgres_helpers",)


async def test_sweeper_enqueues_supported_order_once_and_ignores_mock(
    payment_db: async_sessionmaker[AsyncSession],
) -> None:
    _, stripe_id = await create_order(payment_db, provider="stripe")
    _, mock_id = await create_order(payment_db, provider="mock")
    async with payment_db.begin() as session:
        for order_id in (stripe_id, mock_id):
            order = await session.get(PaymentOrder, order_id)
            assert order is not None
            order.updated_at = datetime.now(UTC) - timedelta(hours=1)
    sweeper = PaymentReconciliationSweeper(payment_db, 60, {"stripe"})
    assert await sweeper.enqueue_stale() == 1
    assert await sweeper.enqueue_stale() == 0
    async with payment_db() as session:
        jobs = await session.scalar(select(func.count()).select_from(BillingJob))
        job = await session.scalar(select(BillingJob))
    assert jobs == 1 and job is not None and job.object_id == str(stripe_id)
