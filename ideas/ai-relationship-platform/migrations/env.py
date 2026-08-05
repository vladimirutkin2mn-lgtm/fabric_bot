"""Alembic async migration environment."""

import asyncio
import os
import re
from logging.config import fileConfig

from alembic import context
from sqlalchemy import pool, text
from sqlalchemy.engine import Connection
from sqlalchemy.ext.asyncio import async_engine_from_config

from app.config import get_settings
from app.db.analytics import AnalyticsEvent  # noqa: F401
from app.db.base import Base
from app.db.fsm_models import TelegramFSMState  # noqa: F401
from app.db.models import User  # noqa: F401
from app.db.telegram_models import TelegramUpdateInbox  # noqa: F401

config = context.config
if config.config_file_name is not None:
    fileConfig(config.config_file_name)

config.set_main_option("sqlalchemy.url", str(get_settings().database_url))
target_metadata = Base.metadata
migration_schema = os.getenv("MIGRATION_SCHEMA")
if migration_schema is not None and re.fullmatch(r"[a-z][a-z0-9_]{0,62}", migration_schema) is None:
    raise ValueError("MIGRATION_SCHEMA must be a safe PostgreSQL identifier")


def do_run_migrations(connection: Connection) -> None:
    context.configure(connection=connection, target_metadata=target_metadata, compare_type=True)
    with context.begin_transaction():
        context.run_migrations()


async def run_async_migrations() -> None:
    connectable = async_engine_from_config(
        config.get_section(config.config_ini_section, {}),
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
    )
    async with connectable.connect() as connection:
        if migration_schema is not None:
            # The schema is created and owned by the caller. Keeping both application
            # objects and alembic_version on this search path isolates the migration run.
            await connection.execute(text(f'SET search_path TO "{migration_schema}"'))
            # SET starts an implicit transaction; commit it so Alembic owns and
            # commits the following migration transaction normally.
            await connection.commit()
        await connection.run_sync(do_run_migrations)
    await connectable.dispose()


if context.is_offline_mode():
    context.configure(url=config.get_main_option("sqlalchemy.url"), target_metadata=target_metadata)
    with context.begin_transaction():
        context.run_migrations()
else:
    asyncio.run(run_async_migrations())
