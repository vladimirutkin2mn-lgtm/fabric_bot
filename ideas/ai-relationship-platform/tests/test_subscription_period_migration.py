"""Migration safety for durable subscription billing periods."""

import asyncio
import os
import subprocess
from uuid import uuid4

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine

pytestmark = pytest.mark.postgres


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
            return await connection.scalar(text(statement))
    finally:
        await engine.dispose()


def _database_url() -> str:
    url = os.getenv("TEST_DATABASE_URL")
    if not url:
        pytest.skip("TEST_DATABASE_URL is required")
    return url


def _environment(url: str, schema: str) -> dict[str, str]:
    return {
        **os.environ,
        "DATABASE_URL": url,
        "MIGRATION_SCHEMA": schema,
        "TELEGRAM_BOT_TOKEN": "123456789:AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA",
        "CONTENT_ENCRYPTION_KEY": "migration-test-key-material",
        "APP_ENV": "test",
    }


def _schema(url: str) -> str:
    schema = f"subscription_period_{uuid4().hex}"
    asyncio.run(_execute(url, "public", f'CREATE SCHEMA "{schema}"'))
    return schema


def test_subscription_period_migration_round_trip_when_empty() -> None:
    url = _database_url()
    schema = _schema(url)
    environment = _environment(url, schema)
    try:
        subprocess.run(("alembic", "upgrade", "head"), check=True, env=environment)
        assert asyncio.run(_scalar(url, schema, "SELECT version_num FROM alembic_version")) == (
            "20260805_12"
        )
        assert (
            asyncio.run(
                _scalar(url, schema, "SELECT to_regclass('subscription_periods') IS NOT NULL")
            )
            is True
        )
        subprocess.run(("alembic", "downgrade", "20260804_11"), check=True, env=environment)
        assert (
            asyncio.run(_scalar(url, schema, "SELECT to_regclass('subscription_periods') IS NULL"))
            is True
        )
        subprocess.run(("alembic", "upgrade", "head"), check=True, env=environment)
    finally:
        asyncio.run(_execute(url, "public", f'DROP SCHEMA IF EXISTS "{schema}" CASCADE'))


def test_subscription_period_downgrade_refuses_financial_state() -> None:
    url = _database_url()
    schema = _schema(url)
    environment = _environment(url, schema)
    user_id, customer_id, subscription_id, period_id = (uuid4() for _ in range(4))
    try:
        subprocess.run(("alembic", "upgrade", "head"), check=True, env=environment)
        asyncio.run(
            _execute(
                url,
                schema,
                "INSERT INTO users (id,telegram_user_id,first_name,privacy_status) "
                f"VALUES ('{user_id}',991001,'Subscriber','active')",
            )
        )
        asyncio.run(
            _execute(
                url,
                schema,
                "INSERT INTO billing_customers (id,user_id,provider,provider_customer_id) "
                f"VALUES ('{customer_id}','{user_id}','stripe','cus-migration')",
            )
        )
        asyncio.run(
            _execute(
                url,
                schema,
                "INSERT INTO subscriptions "
                "(id,user_id,billing_customer_id,provider,provider_subscription_id,product_code,"
                "product_version,status,consent_version,consented_at) VALUES "
                f"('{subscription_id}','{user_id}','{customer_id}','stripe','sub-migration',"
                "'subscription_monthly',1,'active','billing-v1',now())",
            )
        )
        asyncio.run(
            _execute(
                url,
                schema,
                "INSERT INTO subscription_periods "
                "(id,subscription_id,provider,period_key,status,period_start,period_end,credits,"
                "amount_minor,currency,idempotency_key) VALUES "
                f"('{period_id}','{subscription_id}','stripe','2026-08','pending',"
                "'2026-08-01T00:00:00+00:00','2026-09-01T00:00:00+00:00',30,990,'EUR',"
                "'subscription:period:migration')",
            )
        )
        failed = subprocess.run(
            ("alembic", "downgrade", "20260804_11"),
            env=environment,
            capture_output=True,
            text=True,
        )
        assert failed.returncode != 0
        assert "downgrade refused" in failed.stderr
        assert asyncio.run(_scalar(url, schema, "SELECT version_num FROM alembic_version")) == (
            "20260805_12"
        )
    finally:
        asyncio.run(_execute(url, "public", f'DROP SCHEMA IF EXISTS "{schema}" CASCADE'))
