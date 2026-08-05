"""PostgreSQL migration safety for append-only release gate evidence."""

import asyncio
import os
import subprocess
from typing import cast
from uuid import uuid4

import pytest
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import create_async_engine

pytestmark = pytest.mark.postgres
_HEAD = "20260805_16"
_PARENT = "20260805_15"


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
    schema = f"release_gate_{uuid4().hex}"
    asyncio.run(_execute(url, "public", f'CREATE SCHEMA "{schema}"'))
    return schema


def _insert_attestation(url: str, schema: str) -> str:
    attestation_id = str(uuid4())
    asyncio.run(
        _execute(
            url,
            schema,
            "INSERT INTO release_gate_attestations "
            "(id,gate_name,status,checklist_version,app_env,code_sha,schema_revision,evidence_ref) "
            f"VALUES ('{attestation_id}','stripe_subscription_sandbox','passed','m5-live-v1',"
            "'staging','aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa','20260805_16','staging/run-1')",
        )
    )
    return attestation_id


def test_release_gate_migration_round_trip_when_empty() -> None:
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
    finally:
        asyncio.run(_execute(url, "public", f'DROP SCHEMA IF EXISTS "{schema}" CASCADE'))


def test_release_gate_rows_are_immutable_and_block_downgrade() -> None:
    url = _database_url()
    schema = _schema(url)
    environment = _environment(url, schema)
    try:
        subprocess.run(("alembic", "upgrade", "head"), check=True, env=environment)
        attestation_id = _insert_attestation(url, schema)
        with pytest.raises(DBAPIError, match="append-only"):
            asyncio.run(
                _execute(
                    url,
                    schema,
                    "UPDATE release_gate_attestations SET status='failed' "
                    f"WHERE id='{attestation_id}'",
                )
            )
        assert (
            asyncio.run(
                _scalar(
                    url,
                    schema,
                    f"SELECT status FROM release_gate_attestations WHERE id='{attestation_id}'",
                )
            )
            == "passed"
        )
        failed = subprocess.run(
            ("alembic", "downgrade", _PARENT),
            env=environment,
            capture_output=True,
            text=True,
        )
        assert failed.returncode != 0
        assert "downgrade refused" in failed.stderr
        assert asyncio.run(_scalar(url, schema, "SELECT version_num FROM alembic_version")) == _HEAD
    finally:
        asyncio.run(_execute(url, "public", f'DROP SCHEMA IF EXISTS "{schema}" CASCADE'))
