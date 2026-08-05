"""Database connectivity and Alembic revision readiness checks."""

from dataclasses import dataclass
from functools import lru_cache
from typing import Literal

from alembic.config import Config
from alembic.script import ScriptDirectory
from alembic.util.exc import CommandError
from sqlalchemy import text
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncEngine

SchemaHealthReason = Literal[
    "ok",
    "database_unavailable",
    "schema_unavailable",
    "schema_outdated",
]


class SchemaMetadataError(RuntimeError):
    """Raised when the application cannot determine its packaged Alembic heads."""


@dataclass(frozen=True)
class DatabaseSchemaHealth:
    """Safe readiness state without database or migration exception details."""

    database_available: bool
    schema_current: bool
    reason: SchemaHealthReason
    current_heads: tuple[str, ...] = ()


@lru_cache
def expected_schema_heads(config_path: str = "alembic.ini") -> tuple[str, ...]:
    """Read and cache the migration heads packaged with the running application."""
    try:
        heads = tuple(sorted(ScriptDirectory.from_config(Config(config_path)).get_heads()))
    except (CommandError, OSError) as exc:
        raise SchemaMetadataError("Alembic metadata is unavailable") from exc
    if not heads:
        raise SchemaMetadataError("Alembic metadata has no head revision")
    return heads


async def database_schema_health(
    engine: AsyncEngine,
    expected_heads: tuple[str, ...],
) -> DatabaseSchemaHealth:
    """Verify both a database round trip and exact Alembic head equality."""
    try:
        async with engine.connect() as connection:
            try:
                await connection.execute(text("SELECT 1"))
            except SQLAlchemyError:
                return DatabaseSchemaHealth(False, False, "database_unavailable")
            try:
                result = await connection.execute(text("SELECT version_num FROM alembic_version"))
            except SQLAlchemyError:
                return DatabaseSchemaHealth(True, False, "schema_unavailable")
    except SQLAlchemyError:
        return DatabaseSchemaHealth(False, False, "database_unavailable")

    current_heads = tuple(sorted(str(value) for value in result.scalars().all()))
    if current_heads != tuple(sorted(expected_heads)):
        return DatabaseSchemaHealth(True, False, "schema_outdated", current_heads)
    return DatabaseSchemaHealth(True, True, "ok", current_heads)
