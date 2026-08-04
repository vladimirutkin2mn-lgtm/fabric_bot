"""Data-bearing Alembic privacy downgrade refusal."""

import asyncio
import os
import subprocess
from uuid import uuid4

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine

pytestmark = pytest.mark.postgres
_PRIVACY_PARENT = "20260803_07"
_HEAD_REVISION = "20260804_10"


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


def _environment(url: str, schema: str) -> dict[str, str]:
    return {
        **os.environ,
        "DATABASE_URL": url,
        "MIGRATION_SCHEMA": schema,
        "TELEGRAM_BOT_TOKEN": "123456789:AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA",
        "CONTENT_ENCRYPTION_KEY": "migration-test-key-material",
        "APP_ENV": "test",
    }


def _database_url() -> str:
    url = os.getenv("TEST_DATABASE_URL")
    if not url:
        pytest.skip("TEST_DATABASE_URL is required")
    return url


def _create_schema(url: str, prefix: str) -> str:
    schema = f"{prefix}_{uuid4().hex}"
    asyncio.run(_execute(url, "public", f'CREATE SCHEMA "{schema}"'))
    return schema


def _upgrade_head(environment: dict[str, str]) -> None:
    subprocess.run(("alembic", "upgrade", "head"), check=True, env=environment)


def _privacy_downgrade(environment: dict[str, str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ("alembic", "downgrade", _PRIVACY_PARENT),
        env=environment,
        capture_output=True,
        text=True,
    )


def _assert_privacy_refused(
    url: str,
    schema: str,
    failed: subprocess.CompletedProcess[str],
) -> None:
    assert failed.returncode != 0
    assert "downgrade refused" in failed.stderr
    # PostgreSQL rolls back the complete multi-revision downgrade transaction,
    # including the attempted post-privacy downgrades that precede the M6 guard.
    assert asyncio.run(_scalar(url, schema, "SELECT version_num FROM alembic_version")) == (
        _HEAD_REVISION
    )


def test_clean_privacy_migration_upgrade_downgrade_upgrade() -> None:
    url = _database_url()
    schema = _create_schema(url, "privacy_migration_clean")
    environment = _environment(url, schema)
    try:
        _upgrade_head(environment)
        subprocess.run(("alembic", "downgrade", _PRIVACY_PARENT), check=True, env=environment)
        _upgrade_head(environment)
    finally:
        asyncio.run(_execute(url, "public", f'DROP SCHEMA IF EXISTS "{schema}" CASCADE'))


@pytest.mark.parametrize("ciphertext_column", ["source_ciphertext", "result_ciphertext"])
def test_encrypted_content_refuses_downgrade_before_destructive_ddl(
    ciphertext_column: str,
) -> None:
    url = _database_url()
    schema = _create_schema(url, "privacy_migration")
    user_id, analysis_id = uuid4(), uuid4()
    environment = _environment(url, schema)
    try:
        _upgrade_head(environment)
        asyncio.run(
            _execute(
                url,
                schema,
                "INSERT INTO users (id,telegram_user_id,first_name,privacy_status) "
                f"VALUES ('{user_id}',970001,'Migration','active')",
            )
        )
        asyncio.run(
            _execute(
                url,
                schema,
                "INSERT INTO analyses "
                "(id,user_id,status,intake_step,source_type,message_count,character_count,"
                "llm_attempt_count,report_access,cost_units) "
                f"VALUES ('{analysis_id}','{user_id}','draft','complete','text',0,0,0,'none',0)",
            )
        )
        asyncio.run(
            _execute(
                url,
                schema,
                f"INSERT INTO analysis_private_content (analysis_id,{ciphertext_column}) "
                f"VALUES ('{analysis_id}',decode('010203','hex'))",
            )
        )
        failed = _privacy_downgrade(environment)
        _assert_privacy_refused(url, schema, failed)
        assert (
            asyncio.run(
                _scalar(
                    url,
                    schema,
                    f"SELECT encode({ciphertext_column},'hex') FROM analysis_private_content "
                    f"WHERE analysis_id='{analysis_id}'",
                )
            )
            == "010203"
        )
    finally:
        asyncio.run(_execute(url, "public", f'DROP SCHEMA IF EXISTS "{schema}" CASCADE'))


@pytest.mark.parametrize("reference", ["none", "ledger", "order"])
def test_tombstone_and_financial_references_refuse_downgrade(reference: str) -> None:
    url = _database_url()
    schema = _create_schema(url, "privacy_tombstone")
    user_id, reference_id = uuid4(), uuid4()
    environment = _environment(url, schema)
    try:
        _upgrade_head(environment)
        asyncio.run(
            _execute(
                url,
                schema,
                "INSERT INTO users (id,telegram_user_id,first_name,privacy_status) "
                f"VALUES ('{user_id}',970002,'Migration','active')",
            )
        )
        if reference == "ledger":
            asyncio.run(
                _execute(
                    url,
                    schema,
                    "INSERT INTO credit_transactions "
                    "(id,user_id,type,amount,idempotency_key) "
                    f"VALUES ('{reference_id}','{user_id}','grant',1,"
                    f"'migration-ledger-{reference_id}')",
                )
            )
        elif reference == "order":
            asyncio.run(
                _execute(
                    url,
                    schema,
                    "INSERT INTO payment_orders "
                    "(id,user_id,provider,product_code,status,credits,amount_minor,currency,"
                    "checkout_token,mode,market,product_version,commercial_snapshot) "
                    f"VALUES ('{reference_id}','{user_id}','stripe','analysis_single','pending',"
                    f"1,500,'EUR','{uuid4()}','one_time','INTERNATIONAL',1,'{{}}')",
                )
            )
        asyncio.run(
            _execute(
                url,
                schema,
                "UPDATE users SET telegram_user_id=NULL,telegram_username=NULL,first_name=NULL,"
                "telegram_language=NULL,privacy_status='deleted',deleted_at=now() "
                f"WHERE id='{user_id}'",
            )
        )
        failed = _privacy_downgrade(environment)
        _assert_privacy_refused(url, schema, failed)
        assert (
            asyncio.run(
                _scalar(url, schema, f"SELECT privacy_status FROM users WHERE id='{user_id}'")
            )
            == "deleted"
        )
        if reference != "none":
            table = "credit_transactions" if reference == "ledger" else "payment_orders"
            assert (
                asyncio.run(
                    _scalar(url, schema, f"SELECT count(*) FROM {table} WHERE id='{reference_id}'")
                )
                == 1
            )
    finally:
        asyncio.run(_execute(url, "public", f'DROP SCHEMA IF EXISTS "{schema}" CASCADE'))


@pytest.mark.parametrize(
    "identity_column",
    ["provider_checkout_id", "provider_payment_id", "provider_event_id"],
)
def test_cross_provider_order_identity_refuses_legacy_unique_downgrade(
    identity_column: str,
) -> None:
    url = _database_url()
    schema = _create_schema(url, "privacy_identity")
    user_ids, order_ids = (uuid4(), uuid4()), (uuid4(), uuid4())
    environment = _environment(url, schema)
    try:
        _upgrade_head(environment)
        for index, provider in enumerate(("stripe", "yookassa")):
            asyncio.run(
                _execute(
                    url,
                    schema,
                    "INSERT INTO users (id,telegram_user_id,first_name,privacy_status) "
                    f"VALUES ('{user_ids[index]}',{970010 + index},'Migration','active')",
                )
            )
            asyncio.run(
                _execute(
                    url,
                    schema,
                    "INSERT INTO payment_orders "
                    "(id,user_id,provider,product_code,status,credits,amount_minor,currency,"
                    "checkout_token,mode,market,product_version,commercial_snapshot,"
                    f"{identity_column}) VALUES ('{order_ids[index]}','{user_ids[index]}',"
                    f"'{provider}','analysis_single','pending',1,500,'EUR','{uuid4()}',"
                    "'one_time','INTERNATIONAL',1,'{}','shared-identity')",
                )
            )
        failed = _privacy_downgrade(environment)
        _assert_privacy_refused(url, schema, failed)
        assert asyncio.run(_scalar(url, schema, "SELECT count(*) FROM payment_orders")) == 2
    finally:
        asyncio.run(_execute(url, "public", f'DROP SCHEMA IF EXISTS "{schema}" CASCADE'))


def test_cross_provider_ledger_identity_refuses_legacy_unique_downgrade() -> None:
    url = _database_url()
    schema = _create_schema(url, "privacy_ledger_identity")
    environment = _environment(url, schema)
    try:
        _upgrade_head(environment)
        for index, provider in enumerate(("stripe", "yookassa")):
            user_id, transaction_id = uuid4(), uuid4()
            asyncio.run(
                _execute(
                    url,
                    schema,
                    "INSERT INTO users (id,telegram_user_id,first_name,privacy_status) "
                    f"VALUES ('{user_id}',{970020 + index},'Migration','active')",
                )
            )
            asyncio.run(
                _execute(
                    url,
                    schema,
                    "INSERT INTO credit_transactions "
                    "(id,user_id,type,amount,idempotency_key,external_payment_provider,"
                    f"external_payment_id) VALUES ('{transaction_id}','{user_id}','grant',1,"
                    f"'migration-ledger-{transaction_id}','{provider}','shared-ledger-id')",
                )
            )
        failed = _privacy_downgrade(environment)
        _assert_privacy_refused(url, schema, failed)
        assert asyncio.run(_scalar(url, schema, "SELECT count(*) FROM credit_transactions")) == 2
    finally:
        asyncio.run(_execute(url, "public", f'DROP SCHEMA IF EXISTS "{schema}" CASCADE'))
