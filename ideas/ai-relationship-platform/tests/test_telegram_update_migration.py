"""Data-bearing Telegram inbox migration and deletion-trigger tests."""

import asyncio
import os
import subprocess
from uuid import uuid4

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine

pytestmark = pytest.mark.postgres


async def _execute(engine: AsyncEngine, statement: str) -> None:
    async with engine.begin() as connection:
        await connection.execute(text(statement))


async def _rows(engine: AsyncEngine, statement: str) -> list[tuple[object, ...]]:
    async with engine.connect() as connection:
        return list((await connection.execute(text(statement))).tuples())


def test_account_deletion_trigger_scrubs_active_and_terminal_telegram_identity() -> None:
    url = os.getenv("TEST_DATABASE_URL")
    if not url:
        pytest.skip("TEST_DATABASE_URL is required")
    schema = f"telegram_migration_{uuid4().hex}"
    admin = create_async_engine(url)
    engine = create_async_engine(
        url,
        connect_args={"server_settings": {"search_path": schema}},
    )
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
        asyncio.run(_execute(admin, f'CREATE SCHEMA "{schema}"'))
        subprocess.run(("alembic", "upgrade", "head"), check=True, env=environment)
        asyncio.run(
            _execute(
                engine,
                "INSERT INTO users (id,telegram_user_id,first_name,privacy_status) "
                f"VALUES ('{user_id}',980001,'Migration','active')",
            )
        )
        asyncio.run(
            _execute(
                engine,
                "INSERT INTO telegram_update_inbox "
                "(update_id,telegram_user_id,payload_ciphertext,payload_hash,status,attempt_count) "
                "VALUES (3001,980001,decode('010203','hex'),'a','pending',0),"
                "(3002,980001,NULL,'b','completed',1)",
            )
        )
        asyncio.run(
            _execute(
                engine,
                "UPDATE users SET telegram_user_id=NULL,privacy_status='deleted',deleted_at=now() "
                f"WHERE id='{user_id}'",
            )
        )
        rows = asyncio.run(
            _rows(
                engine,
                "SELECT update_id,telegram_user_id,status,payload_ciphertext,last_error_code "
                "FROM telegram_update_inbox ORDER BY update_id",
            )
        )
        assert rows == [
            (3001, None, "failed", None, "user_deleted"),
            (3002, None, "completed", None, None),
        ]
    finally:
        asyncio.run(engine.dispose())
        asyncio.run(_execute(admin, f'DROP SCHEMA IF EXISTS "{schema}" CASCADE'))
        asyncio.run(admin.dispose())
