"""Data-bearing Telegram inbox migration and deletion-trigger tests."""

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


async def _rows(url: str, schema: str, statement: str) -> list[tuple[object, ...]]:
    engine = create_async_engine(url, connect_args={"server_settings": {"search_path": schema}})
    try:
        async with engine.connect() as connection:
            return list((await connection.execute(text(statement))).tuples())
    finally:
        await engine.dispose()


def test_account_deletion_trigger_scrubs_active_and_terminal_telegram_identity() -> None:
    url = os.getenv("TEST_DATABASE_URL")
    if not url:
        pytest.skip("TEST_DATABASE_URL is required")
    schema = f"telegram_migration_{uuid4().hex}"
    environment = {
        **os.environ,
        "DATABASE_URL": url,
        "MIGRATION_SCHEMA": schema,
        "TELEGRAM_BOT_TOKEN": "123456789:AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA",
        "CONTENT_ENCRYPTION_KEY": "migration-test-key-material",
        "APP_ENV": "test",
    }
    user_id = uuid4()
    try:
        asyncio.run(_execute(url, "public", f'CREATE SCHEMA "{schema}"'))
        subprocess.run(("alembic", "upgrade", "head"), check=True, env=environment)
        asyncio.run(
            _execute(
                url,
                schema,
                "INSERT INTO users (id,telegram_user_id,first_name,privacy_status) "
                f"VALUES ('{user_id}',980001,'Migration','active')",
            )
        )
        asyncio.run(
            _execute(
                url,
                schema,
                "INSERT INTO telegram_update_inbox "
                "(update_id,telegram_user_id,payload_ciphertext,payload_hash,status,attempt_count) "
                "VALUES (3001,980001,decode('010203','hex'),'a','pending',0),"
                "(3002,980001,NULL,NULL,'completed',1)",
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
        rows = asyncio.run(
            _rows(
                url,
                schema,
                "SELECT update_id,telegram_user_id,status,payload_ciphertext,payload_hash,"
                "last_error_code FROM telegram_update_inbox ORDER BY update_id",
            )
        )
        assert rows == [
            (3001, None, "failed", None, None, "user_deleted"),
            (3002, None, "completed", None, None, None),
        ]
    finally:
        asyncio.run(_execute(url, "public", f'DROP SCHEMA IF EXISTS "{schema}" CASCADE'))
