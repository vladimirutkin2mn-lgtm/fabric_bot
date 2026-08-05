"""PostgreSQL migration safety for the paid follow-up entitlement."""

import asyncio
import os
import subprocess
from typing import cast
from uuid import uuid4

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine

pytestmark = pytest.mark.postgres
_HEAD = "20260805_16"
_PARENT = "20260805_14"


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
    schema = f"followup_{uuid4().hex}"
    asyncio.run(_execute(url, "public", f'CREATE SCHEMA "{schema}"'))
    return schema


def _insert_available_followup(url: str, schema: str) -> tuple[str, str]:
    user_id, analysis_id, followup_id = uuid4(), uuid4(), uuid4()
    asyncio.run(
        _execute(
            url,
            schema,
            "INSERT INTO users (id,telegram_user_id,first_name,privacy_status) "
            f"VALUES ('{user_id}',{uuid4().int % 10**12},'Migration','active')",
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
            "INSERT INTO analysis_followups "
            "(id,analysis_id,user_id,status,prompt_version,reservation_count,llm_attempt_count) "
            f"VALUES ('{followup_id}','{analysis_id}','{user_id}','available','followup_v1',0,0)",
        )
    )
    return str(user_id), str(analysis_id)


def test_followup_migration_round_trip_when_empty() -> None:
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


def test_followup_downgrade_refuses_live_entitlement_state() -> None:
    url = _database_url()
    schema = _schema(url)
    environment = _environment(url, schema)
    try:
        subprocess.run(("alembic", "upgrade", "head"), check=True, env=environment)
        _insert_available_followup(url, schema)
        failed = subprocess.run(
            ("alembic", "downgrade", _PARENT),
            env=environment,
            capture_output=True,
            text=True,
        )
        assert failed.returncode != 0
        assert "downgrade refused" in failed.stderr
        assert asyncio.run(_scalar(url, schema, "SELECT version_num FROM alembic_version")) == _HEAD
        assert asyncio.run(_scalar(url, schema, "SELECT count(*) FROM analysis_followups")) == 1
    finally:
        asyncio.run(_execute(url, "public", f'DROP SCHEMA IF EXISTS "{schema}" CASCADE'))


def test_soft_deleted_analysis_purges_followup_before_downgrade() -> None:
    url = _database_url()
    schema = _schema(url)
    environment = _environment(url, schema)
    try:
        subprocess.run(("alembic", "upgrade", "head"), check=True, env=environment)
        _, analysis_id = _insert_available_followup(url, schema)
        asyncio.run(
            _execute(
                url,
                schema,
                "UPDATE analyses SET status='deleted',report_access='none',completed_at=NULL "
                f"WHERE id='{analysis_id}'",
            )
        )
        assert asyncio.run(_scalar(url, schema, "SELECT count(*) FROM analysis_followups")) == 0
        subprocess.run(("alembic", "downgrade", _PARENT), check=True, env=environment)
    finally:
        asyncio.run(_execute(url, "public", f'DROP SCHEMA IF EXISTS "{schema}" CASCADE'))
