"""Migration-chain regression isolated from the shared test schema."""

import asyncio
import os
import subprocess
from uuid import uuid4

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine

pytestmark = pytest.mark.postgres


async def _schema_statement(database_url: str, statement: str) -> None:
    engine = create_async_engine(database_url)
    try:
        async with engine.begin() as connection:
            await connection.execute(text(statement))
    finally:
        await engine.dispose()


def test_billing_migration_upgrade_downgrade_upgrade() -> None:
    """Exercise the complete chain in a unique, disposable PostgreSQL schema."""
    url = os.getenv("TEST_DATABASE_URL")
    if not url:
        pytest.skip("TEST_DATABASE_URL is required")
    schema = f"billing_migration_{uuid4().hex}"
    asyncio.run(_schema_statement(url, f'CREATE SCHEMA "{schema}"'))
    environment = {
        **os.environ,
        "DATABASE_URL": url,
        "MIGRATION_SCHEMA": schema,
        "TELEGRAM_BOT_TOKEN": "123456789:AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA",
        "CONTENT_ENCRYPTION_KEY": "migration-test-key",
        "APP_ENV": "test",
    }
    try:
        for arguments in (("upgrade", "head"), ("downgrade", "-1"), ("upgrade", "head")):
            subprocess.run(
                ("alembic", *arguments),
                check=True,
                env=environment,
                capture_output=True,
                text=True,
            )
    finally:
        asyncio.run(_schema_statement(url, f'DROP SCHEMA "{schema}" CASCADE'))
