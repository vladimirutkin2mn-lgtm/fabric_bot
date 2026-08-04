"""Data-bearing Alembic privacy downgrade refusal."""

import asyncio
import os
import subprocess
from uuid import uuid4

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine

pytestmark = pytest.mark.postgres


async def _execute(url: str, schema: str, statement: str) -> object | None:
    engine = create_async_engine(url, connect_args={"server_settings": {"search_path": schema}})
    try:
        async with engine.begin() as connection:
            result: object | None = await connection.scalar(text(statement))
            return result
    finally:
        await engine.dispose()


def test_encrypted_source_refuses_downgrade_before_destructive_ddl() -> None:
    url = os.getenv("TEST_DATABASE_URL")
    if not url:
        pytest.skip("TEST_DATABASE_URL is required")
    schema = f"privacy_migration_{uuid4().hex}"
    user_id, analysis_id = uuid4(), uuid4()
    asyncio.run(_execute(url, "public", f'CREATE SCHEMA "{schema}"'))
    environment = {
        **os.environ,
        "DATABASE_URL": url,
        "MIGRATION_SCHEMA": schema,
        "TELEGRAM_BOT_TOKEN": "123456789:AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA",
        "CONTENT_ENCRYPTION_KEY": "migration-test-key-material",
        "APP_ENV": "test",
    }
    try:
        subprocess.run(("alembic", "upgrade", "head"), check=True, env=environment)
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
                "INSERT INTO analysis_private_content (analysis_id,source_ciphertext) "
                f"VALUES ('{analysis_id}',decode('010203','hex'))",
            )
        )
        failed = subprocess.run(
            ("alembic", "downgrade", "-1"), env=environment, capture_output=True, text=True
        )
        assert failed.returncode != 0
        assert "downgrade refused" in failed.stderr
        assert (
            asyncio.run(
                _execute(
                    url,
                    schema,
                    "SELECT encode(source_ciphertext,'hex') FROM analysis_private_content "
                    f"WHERE analysis_id='{analysis_id}'",
                )
            )
            == "010203"
        )
        assert asyncio.run(_execute(url, schema, "SELECT version_num FROM alembic_version")) == (
            "20260804_08"
        )
    finally:
        asyncio.run(_execute(url, "public", f'DROP SCHEMA IF EXISTS "{schema}" CASCADE'))
