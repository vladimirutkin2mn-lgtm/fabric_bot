"""Migration safety for purchase refund ledger entries."""

import asyncio
import os
import subprocess
from typing import cast
from uuid import uuid4

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine

pytestmark = pytest.mark.postgres
_HEAD = "20260805_14"
_PARENT = "20260805_13"


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
            return cast(object | None, await connection.scalar(text(statement)))
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
    schema = f"refund_ledger_{uuid4().hex}"
    asyncio.run(_execute(url, "public", f'CREATE SCHEMA "{schema}"'))
    return schema


def _insert_refunded_purchase(url: str, schema: str) -> None:
    user_id = uuid4()
    order_id = uuid4()
    purchase_id = uuid4()
    refund_id = uuid4()
    asyncio.run(
        _execute(
            url,
            schema,
            f"INSERT INTO users (id,telegram_user_id) VALUES ('{user_id}',{uuid4().int % 10**12});"
            "INSERT INTO payment_orders "
            "(id,user_id,provider,product_code,status,credits,amount_minor,currency,checkout_token,"
            "provider_payment_id,provider_status,completed_at,commercial_snapshot) VALUES "
            f"('{order_id}','{user_id}','stripe','analysis_pack_5','completed',5,1000,'EUR',"
            f"'{uuid4()}','payment-migration','succeeded',now(),'{{}}');"
            "INSERT INTO credit_transactions "
            "(id,user_id,type,amount,idempotency_key,payment_order_id,product_code,"
            "external_payment_id,external_payment_provider) VALUES "
            f"('{purchase_id}','{user_id}','purchase',5,'purchase:{order_id}','{order_id}',"
            "'analysis_pack_5','payment-migration','stripe');"
            "INSERT INTO refund_requests "
            "(id,user_id,payment_order_id,provider,provider_refund_id,status,amount_minor,currency,"
            "credit_units,reason,idempotency_key) VALUES "
            f"('{refund_id}','{user_id}','{order_id}','stripe','refund-migration','succeeded',"
            "1000,'EUR',5,'requested_by_customer','refund:migration');"
            "INSERT INTO credit_transactions "
            "(id,user_id,type,amount,idempotency_key,payment_order_id,product_code,"
            "external_payment_id,external_payment_provider,original_purchase_transaction_id,"
            "refund_request_id) VALUES "
            f"('{uuid4()}','{user_id}','purchase_refund',-5,'purchase_refund:{refund_id}',"
            f"'{order_id}','analysis_pack_5','refund-migration','stripe','{purchase_id}',"
            f"'{refund_id}')",
        )
    )


def test_refund_ledger_index_round_trip_when_no_refunds_exist() -> None:
    url = _database_url()
    schema = _schema(url)
    environment = _environment(url, schema)
    try:
        subprocess.run(("alembic", "upgrade", "head"), check=True, env=environment)
        assert asyncio.run(_scalar(url, schema, "SELECT version_num FROM alembic_version")) == _HEAD
        subprocess.run(("alembic", "downgrade", _PARENT), check=True, env=environment)
        assert (
            asyncio.run(_scalar(url, schema, "SELECT version_num FROM alembic_version")) == _PARENT
        )
        subprocess.run(("alembic", "upgrade", "head"), check=True, env=environment)
        _insert_refunded_purchase(url, schema)
        assert (
            asyncio.run(
                _scalar(
                    url,
                    schema,
                    "SELECT count(*) FROM credit_transactions WHERE payment_order_id IS NOT NULL",
                )
            )
            == 2
        )
    finally:
        asyncio.run(_execute(url, "public", f'DROP SCHEMA IF EXISTS "{schema}" CASCADE'))


def test_refund_ledger_downgrade_refuses_live_purchase_refund() -> None:
    url = _database_url()
    schema = _schema(url)
    environment = _environment(url, schema)
    try:
        subprocess.run(("alembic", "upgrade", "head"), check=True, env=environment)
        _insert_refunded_purchase(url, schema)
        failed = subprocess.run(
            ("alembic", "downgrade", _PARENT),
            env=environment,
            capture_output=True,
            text=True,
        )
        assert failed.returncode != 0
        assert "downgrade refused" in failed.stderr
        assert asyncio.run(_scalar(url, schema, "SELECT version_num FROM alembic_version")) == _HEAD
        assert (
            asyncio.run(
                _scalar(
                    url,
                    schema,
                    "SELECT count(*) FROM credit_transactions WHERE type='purchase_refund'",
                )
            )
            == 1
        )
    finally:
        asyncio.run(_execute(url, "public", f'DROP SCHEMA IF EXISTS "{schema}" CASCADE'))
