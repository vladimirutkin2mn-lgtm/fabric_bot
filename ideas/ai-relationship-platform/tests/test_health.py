"""Health endpoint tests."""

from unittest.mock import AsyncMock, MagicMock

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy.exc import OperationalError, ProgrammingError

from app.api.main import create_app
from app.config import Settings

_HEAD = "20260804_11"


def engine_mock(
    *,
    connection_error: Exception | None = None,
    schema_error: Exception | None = None,
    revisions: tuple[str, ...] = (_HEAD,),
) -> MagicMock:
    engine = MagicMock()
    connection_context = MagicMock()
    if connection_error:
        connection_context.__aenter__ = AsyncMock(side_effect=connection_error)
    else:
        connection = MagicMock()
        schema_result = MagicMock()
        schema_result.scalars.return_value.all.return_value = list(revisions)
        if schema_error:
            connection.execute = AsyncMock(side_effect=[MagicMock(), schema_error])
        else:
            connection.execute = AsyncMock(side_effect=[MagicMock(), schema_result])
        connection_context.__aenter__ = AsyncMock(return_value=connection)
    connection_context.__aexit__ = AsyncMock(return_value=None)
    engine.connect.return_value = connection_context
    engine.dispose = AsyncMock()
    return engine


@pytest.mark.asyncio
async def test_liveness(settings: Settings) -> None:
    engine = engine_mock()
    app = create_app(settings, engine, schema_heads=(_HEAD,))
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.get("/health/live")
    assert response.status_code == 200
    assert response.json() == {"status": "ok"}
    engine.connect.assert_not_called()


@pytest.mark.asyncio
async def test_readiness_checks_database_and_schema(settings: Settings) -> None:
    engine = engine_mock()
    app = create_app(settings, engine, schema_heads=(_HEAD,))
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.get("/health/ready")
    assert response.status_code == 200
    assert response.json() == {
        "status": "ok",
        "database": "available",
        "schema": "current",
    }


@pytest.mark.asyncio
async def test_readiness_reports_database_failure(settings: Settings) -> None:
    error = OperationalError("SELECT 1", {}, Exception("offline"))
    app = create_app(
        settings,
        engine_mock(connection_error=error),
        schema_heads=(_HEAD,),
    )
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.get("/health/ready")
    assert response.status_code == 503
    assert response.json() == {"detail": "database unavailable"}


@pytest.mark.asyncio
async def test_readiness_reports_missing_schema_metadata(settings: Settings) -> None:
    error = ProgrammingError(
        "SELECT version_num FROM alembic_version",
        {},
        Exception("missing relation"),
    )
    app = create_app(
        settings,
        engine_mock(schema_error=error),
        schema_heads=(_HEAD,),
    )
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.get("/health/ready")
    assert response.status_code == 503
    assert response.json() == {"detail": "database schema unavailable"}


@pytest.mark.asyncio
async def test_readiness_reports_outdated_schema(settings: Settings) -> None:
    app = create_app(
        settings,
        engine_mock(revisions=("20260804_10",)),
        schema_heads=(_HEAD,),
    )
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.get("/health/ready")
    assert response.status_code == 503
    assert response.json() == {"detail": "database schema is not current"}
