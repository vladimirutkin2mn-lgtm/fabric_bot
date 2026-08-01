"""Kubernetes- and Compose-compatible health endpoints."""

from fastapi import APIRouter, HTTPException, Request, status
from sqlalchemy import text
from sqlalchemy.exc import SQLAlchemyError

router = APIRouter(prefix="/health", tags=["health"])


@router.get("/live")
async def liveness() -> dict[str, str]:
    """Report that the API process is responsive."""
    return {"status": "ok"}


@router.get("/ready")
async def readiness(request: Request) -> dict[str, str]:
    """Report readiness after executing a database round trip."""
    try:
        async with request.app.state.db_engine.connect() as connection:
            await connection.execute(text("SELECT 1"))
    except SQLAlchemyError as exc:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="database unavailable",
        ) from exc
    return {"status": "ok", "database": "available"}
