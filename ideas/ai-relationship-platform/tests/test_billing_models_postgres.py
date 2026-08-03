"""PostgreSQL schema invariants for billing persistence."""

import os
from collections.abc import AsyncIterator

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine

pytestmark = pytest.mark.postgres


@pytest.fixture
async def billing_schema() -> AsyncIterator[AsyncEngine]:
    url = os.getenv("TEST_DATABASE_URL")
    if not url:
        pytest.skip("TEST_DATABASE_URL is required")
    engine = create_async_engine(url)
    yield engine
    await engine.dispose()


async def test_active_subscription_index_and_payload_free_webhook_inbox(
    billing_schema: AsyncEngine,
) -> None:
    async with billing_schema.connect() as connection:
        index_definition = await connection.scalar(
            text(
                "SELECT indexdef FROM pg_indexes "
                "WHERE indexname = 'uq_subscriptions_active_user_product'"
            )
        )
        webhook_columns = set(
            (
                await connection.execute(
                    text(
                        "SELECT column_name FROM information_schema.columns "
                        "WHERE table_name = 'provider_webhook_events'"
                    )
                )
            ).scalars()
        )
    assert index_definition is not None
    for status in ("incomplete", "active", "past_due", "cancel_at_period_end", "paused"):
        assert status in index_definition
    assert "payload_hash" in webhook_columns
    assert "payload" not in webhook_columns
