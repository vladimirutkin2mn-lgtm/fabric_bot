"""Authenticated aggregate-only administration endpoints."""

import hmac
from typing import Annotated, cast

from fastapi import APIRouter, Header, HTTPException, Request, status

from app.observability.settings import ObservabilitySettings
from app.services.admin_metrics import AdminMetrics, AdminMetricsService

router = APIRouter(prefix="/admin", tags=["admin"])


@router.get("/metrics", response_model=AdminMetrics)
async def metrics(
    request: Request,
    admin_token: Annotated[str | None, Header(alias="X-Admin-Token")] = None,
) -> AdminMetrics:
    settings = cast(ObservabilitySettings, request.app.state.observability_settings)
    if not settings.admin_metrics_enabled:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Not found")
    expected = settings.admin_api_token.get_secret_value()
    supplied = admin_token or ""
    if not hmac.compare_digest(supplied.encode(), expected.encode()):
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Unauthorized")
    service = cast(AdminMetricsService, request.app.state.admin_metrics_service)
    return await service.snapshot()
