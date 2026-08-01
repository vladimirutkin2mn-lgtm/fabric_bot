"""Health endpoint tests."""

from unittest.mock import AsyncMock, MagicMock

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy.exc import OperationalError

from app.api.main import create_app
from app.config import Settings


def engine_mock(*, connection_error: Exception | None = None) -> MagicMock:
    engine = MagicMock()
    connection_context = MagicMock()
    if connection_error:
        connection_context.__aenter__ = AsyncMock(side_effect=connection_error)
    else:
        connection = MagicMock()
        connection.execute = AsyncMock()
        connection_context.__aenter__ = AsyncMock(return_value=connection)
    connection_context.__aexit__ = AsyncMock(return_value=None)
    engine.connect.return_value = connection_context
    engine.dispose = AsyncMock()
    return engine


@pytest.mark.asyncio
async def test_liveness(settings: Settings) -> None:
    app = create_app(settings, engine_mock())
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.get("/health/live")
    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


@pytest.mark.asyncio
async def test_readiness_checks_database(settings: Settings) -> None:
    engine = engine_mock()
    app = create_app(settings, engine)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.get("/health/ready")
    assert response.status_code == 200
    assert response.json() == {"status": "ok", "database": "available"}


@pytest.mark.asyncio
async def test_readiness_reports_database_failure(settings: Settings) -> None:
    error = OperationalError("SELECT 1", {}, Exception("offline"))
    app = create_app(settings, engine_mock(connection_error=error))
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.get("/health/ready")
    assert response.status_code == 503
    assert response.json() == {"detail": "database unavailable"}
