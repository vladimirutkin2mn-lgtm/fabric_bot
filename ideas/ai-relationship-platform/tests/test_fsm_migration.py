"""Data-bearing durable FSM migration and deletion-trigger tests."""

import asyncio
import os
import subprocess
from uuid import uuid4

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine

pytestmark = pytest.mark.postgres
_HEAD_REVISION = "20260805_12"


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


def _create_schema(url: str, prefix: str) -> str:
    schema = f"{prefix}_{uuid4().hex}"
    asyncio.run(_execute(url, "public", f'CREATE SCHEMA "{schema}"'))
    return schema


def test_account_deletion_trigger_removes_fsm_state() -> None:
    url = _database_url()
    schema = _create_schema(url, "fsm_delete")
    environment = _environment(url, schema)
    user_id = uuid4()
    try:
        subprocess.run(("alembic", "upgrade", "head"), check=True, env=environment)
        asyncio.run(
            _execute(
                url,
                schema,
                "INSERT INTO users (id,telegram_user_id,first_name,privacy_status) "
                f"VALUES ('{user_id}',990001,'Migration','active')",
            )
        )
        asyncio.run(
            _execute(
                url,
                schema,
                "INSERT INTO telegram_fsm_state "
                "(bot_id,chat_id,user_id,thread_id,business_connection_id,destiny,state,"
                "data_ciphertext) VALUES "
                "(123456789,990001,990001,0,'','default','IntakeStates:waiting_for_goal',"
                "decode('010203','hex'))",
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
        assert asyncio.run(_scalar(url, schema, "SELECT count(*) FROM telegram_fsm_state")) == 0
        assert asyncio.run(_scalar(url, schema, "SELECT version_num FROM alembic_version")) == (
            _HEAD_REVISION
        )
    finally:
        asyncio.run(_execute(url, "public", f'DROP SCHEMA IF EXISTS "{schema}" CASCADE'))


def test_downgrade_refuses_to_destroy_live_fsm_state() -> None:
    url = _database_url()
    schema = _create_schema(url, "fsm_downgrade")
    environment = _environment(url, schema)
    try:
        subprocess.run(("alembic", "upgrade", "head"), check=True, env=environment)
        asyncio.run(
            _execute(
                url,
                schema,
                "INSERT INTO telegram_fsm_state "
                "(bot_id,chat_id,user_id,thread_id,business_connection_id,destiny,state) "
                "VALUES (123456789,990002,990002,0,'','default',"
                "'OnboardingStates:waiting_for_consent')",
            )
        )
        failed = subprocess.run(
            ("alembic", "downgrade", "20260804_10"),
            env=environment,
            capture_output=True,
            text=True,
        )
        assert failed.returncode != 0
        assert "downgrade refused" in failed.stderr
        assert asyncio.run(_scalar(url, schema, "SELECT version_num FROM alembic_version")) == (
            _HEAD_REVISION
        )
        assert asyncio.run(_scalar(url, schema, "SELECT count(*) FROM telegram_fsm_state")) == 1
    finally:
        asyncio.run(_execute(url, "public", f'DROP SCHEMA IF EXISTS "{schema}" CASCADE'))
