"""Run Alembic once under a PostgreSQL advisory deployment lock."""

import asyncio
import logging

import asyncpg  # type: ignore[import-untyped]
from alembic import command
from alembic.config import Config

from app.config import get_settings
from app.logging import configure_logging

logger = logging.getLogger(__name__)
_MIGRATION_LOCK_ID = 2_026_080_408


def asyncpg_dsn(database_url: str) -> str:
    """Convert managed or SQLAlchemy PostgreSQL URLs to an asyncpg-compatible DSN."""
    if database_url.startswith("postgres://"):
        return database_url.replace("postgres://", "postgresql://", 1)
    return database_url.replace("postgresql+asyncpg://", "postgresql://", 1)


async def run() -> None:
    """Serialize release migrations and fail the deployment on any migration error."""
    settings = get_settings()
    configure_logging(settings.log_level)
    connection = await asyncpg.connect(asyncpg_dsn(str(settings.database_url)))
    try:
        await connection.execute("SELECT pg_advisory_lock($1)", _MIGRATION_LOCK_ID)
        logger.info("migration_lock_acquired")
        await asyncio.to_thread(command.upgrade, Config("alembic.ini"), "head")
        logger.info("migration_upgrade_completed")
    finally:
        try:
            await connection.execute("SELECT pg_advisory_unlock($1)", _MIGRATION_LOCK_ID)
        finally:
            await connection.close()


def main() -> None:
    asyncio.run(run())


if __name__ == "__main__":
    main()
