"""Kubernetes- and Compose-compatible health endpoints."""

from fastapi import APIRouter, HTTPException, Request, status

from app.services.schema_health import (
    SchemaMetadataError,
    database_schema_health,
    expected_schema_heads,
)

router = APIRouter(prefix="/health", tags=["health"])


@router.get("/live")
async def liveness() -> dict[str, str]:
    """Report that the API process is responsive."""
    return {"status": "ok"}


@router.get("/ready")
async def readiness(request: Request) -> dict[str, str]:
    """Require database connectivity and the exact packaged Alembic head."""
    configured_heads: tuple[str, ...] | None = request.app.state.expected_schema_heads
    try:
        required_heads = configured_heads or expected_schema_heads()
    except SchemaMetadataError as exc:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="schema metadata unavailable",
        ) from exc

    health = await database_schema_health(request.app.state.db_engine, required_heads)
    if health.reason == "database_unavailable":
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="database unavailable",
        )
    if health.reason == "schema_unavailable":
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="database schema unavailable",
        )
    if health.reason == "schema_outdated":
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="database schema is not current",
        )
    return {"status": "ok", "database": "available", "schema": "current"}
