"""PostgreSQL coverage for durable privacy-safe analytics."""

import asyncio
import os
import subprocess
from collections.abc import AsyncIterator
from uuid import UUID, uuid4

import pytest
from sqlalchemy import func, select, text
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from app.db.analytics import AnalyticsEvent
from app.db.base import Base
from app.observability.context import reset_correlation_id, set_correlation_id
from app.providers.analytics import AnalyticsContractError
from app.providers.analytics_postgres import PostgresAnalyticsClient

pytestmark = pytest.mark.postgres


@pytest.fixture
async def analytics_postgres() -> AsyncIterator[
    tuple[AsyncEngine, async_sessionmaker[AsyncSession]]
]:
    url = os.getenv("TEST_DATABASE_URL")
    if not url:
        pytest.skip("TEST_DATABASE_URL is required")
    engine = create_async_engine(url)
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.drop_all)
        await connection.run_sync(Base.metadata.create_all)
    yield engine, async_sessionmaker(engine, expire_on_commit=False)
    await engine.dispose()


async def test_durable_transition_events_are_idempotent_per_entity(
    analytics_postgres: tuple[AsyncEngine, async_sessionmaker[AsyncSession]],
) -> None:
    _, sessions = analytics_postgres
    client = PostgresAnalyticsClient(sessions)
    user_id, first_id, second_id = str(uuid4()), str(uuid4()), str(uuid4())

    _, token = set_correlation_id("request-transition")
    try:
        await client.track(user_id, "analysis_completed", {"analysis_id": first_id})
        await client.track(user_id, "analysis_completed", {"analysis_id": first_id})
        await client.track(user_id, "analysis_completed", {"analysis_id": second_id})
    finally:
        reset_correlation_id(token)

    async with sessions() as session:
        rows = list(
            (
                await session.scalars(
                    select(AnalyticsEvent).order_by(AnalyticsEvent.idempotency_key)
                )
            ).all()
        )
    assert len(rows) == 2
    assert {row.idempotency_key for row in rows} == {
        f"analysis_completed:{first_id}",
        f"analysis_completed:{second_id}",
    }
    assert all(row.subject_id == user_id for row in rows)
    assert all(row.correlation_id == "request-transition" for row in rows)


async def test_action_events_are_distinguished_by_correlation_id(
    analytics_postgres: tuple[AsyncEngine, async_sessionmaker[AsyncSession]],
) -> None:
    _, sessions = analytics_postgres
    client = PostgresAnalyticsClient(sessions)
    user_id, analysis_id = str(uuid4()), str(uuid4())

    for correlation_id in ("action-one", "action-two"):
        _, token = set_correlation_id(correlation_id)
        try:
            await client.track(
                user_id,
                "reply_suggestions_requested",
                {"analysis_id": analysis_id},
            )
        finally:
            reset_correlation_id(token)

    async with sessions() as session:
        rows = list((await session.scalars(select(AnalyticsEvent))).all())
    assert {row.correlation_id for row in rows} == {"action-one", "action-two"}
    assert {row.idempotency_key for row in rows} == {
        "reply_suggestions_requested:action-one",
        "reply_suggestions_requested:action-two",
    }


async def test_forbidden_analytics_data_is_not_persisted_or_echoed(
    analytics_postgres: tuple[AsyncEngine, async_sessionmaker[AsyncSession]],
) -> None:
    _, sessions = analytics_postgres
    client = PostgresAnalyticsClient(sessions)
    sentinel = "private-conversation-sentinel"
    with pytest.raises(AnalyticsContractError) as raised:
        await client.track(
            str(uuid4()),
            "analysis_completed",
            {"analysis_id": str(uuid4()), "report_text": sentinel},
        )
    assert sentinel not in str(raised.value)
    async with sessions() as session:
        assert await session.scalar(select(func.count()).select_from(AnalyticsEvent)) == 0


async def _execute(url: str, schema: str, statement: str) -> None:
    engine = create_async_engine(url, connect_args={"server_settings": {"search_path": schema}})
    try:
        async with engine.begin() as connection:
            await connection.execute(text(statement))
    finally:
        await engine.dispose()


async def _scalar(url: str, schema: str, statement: str) -> object | None:
    engine = create_async_engine(url, connect_args={"server_settings": {"search_path": schema}})
    try:
        async with engine.connect() as connection:
            result: object | None = await connection.scalar(text(statement))
            return result
    finally:
        await engine.dispose()


async def _rolled_back_projection(url: str, schema: str, outbox_id: UUID) -> None:
    engine = create_async_engine(url, connect_args={"server_settings": {"search_path": schema}})
    try:
        async with engine.connect() as connection:
            transaction = await connection.begin()
            await connection.execute(
                text(
                    "INSERT INTO billing_outbox_events "
                    "(id,aggregate_type,aggregate_id,event_type,payload,idempotency_key,"
                    "status,attempt_count,available_at,created_at,updated_at) VALUES "
                    "(:id,'payment_order',:aggregate_id,'purchase_completed',"
                    "CAST(:payload AS jsonb),:key,'pending',0,now(),now(),now())"
                ),
                {
                    "id": outbox_id,
                    "aggregate_id": str(uuid4()),
                    "payload": '{"product_code":"analysis_single","provider":"stripe",'
                    '"market":"RU","currency":"RUB","credits":"1",'
                    '"private_text":"must-not-project"}',
                    "key": f"purchase-completed:{outbox_id}",
                },
            )
            assert await connection.scalar(text("SELECT count(*) FROM analytics_events")) == 1
            await transaction.rollback()
    finally:
        await engine.dispose()


def _environment(url: str, schema: str) -> dict[str, str]:
    return {
        **os.environ,
        "DATABASE_URL": url,
        "MIGRATION_SCHEMA": schema,
        "TELEGRAM_BOT_TOKEN": "123456789:AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA",
        "CONTENT_ENCRYPTION_KEY": "analytics-migration-test-key",
        "APP_ENV": "test",
    }


def test_billing_outbox_projection_is_transactional_and_allow_listed() -> None:
    url = os.getenv("TEST_DATABASE_URL")
    if not url:
        pytest.skip("TEST_DATABASE_URL is required")
    schema = f"analytics_projection_{uuid4().hex}"
    rolled_back_id, committed_id = uuid4(), uuid4()
    asyncio.run(_execute(url, "public", f'CREATE SCHEMA "{schema}"'))
    environment = _environment(url, schema)
    try:
        subprocess.run(("alembic", "upgrade", "head"), check=True, env=environment)
        asyncio.run(_rolled_back_projection(url, schema, rolled_back_id))
        assert asyncio.run(_scalar(url, schema, "SELECT count(*) FROM analytics_events")) == 0
        asyncio.run(
            _execute(
                url,
                schema,
                "INSERT INTO billing_outbox_events "
                "(id,aggregate_type,aggregate_id,event_type,payload,idempotency_key,status,"
                "attempt_count,available_at,created_at,updated_at) VALUES "
                f"('{committed_id}','payment_order','{uuid4()}','purchase_completed',"
                '\'{"product_code":"analysis_single","provider":"stripe",'
                '"market":"RU","currency":"RUB","credits":"1",'
                '"private_text":"must-not-project"}\'::jsonb,'
                f"'purchase-completed:{committed_id}','pending',0,now(),now(),now())",
            )
        )
        assert asyncio.run(_scalar(url, schema, "SELECT count(*) FROM analytics_events")) == 1
        properties = asyncio.run(
            _scalar(
                url,
                schema,
                "SELECT properties::text FROM analytics_events "
                "WHERE event_name='purchase_completed'",
            )
        )
        assert isinstance(properties, str)
        assert "analysis_single" in properties
        assert "must-not-project" not in properties
        assert "private_text" not in properties
    finally:
        asyncio.run(_execute(url, "public", f'DROP SCHEMA IF EXISTS "{schema}" CASCADE'))
