"""Authenticated aggregate-only administration endpoints."""

import hmac
from typing import Annotated, cast

from fastapi import APIRouter, Header, HTTPException, Request, status
from sqlalchemy.ext.asyncio import AsyncEngine

from app.config import Settings
from app.db.session import create_session_factory
from app.observability.settings import ObservabilitySettings
from app.services.admin_metrics import AdminMetrics, AdminMetricsService
from app.services.release_readiness import (
    ReleaseGateAttestationRequest,
    ReleaseGateError,
    ReleaseGateName,
    ReleaseReadiness,
    ReleaseReadinessService,
)

router = APIRouter(prefix="/admin", tags=["admin"])


def _require_admin(request: Request, supplied_token: str | None) -> ObservabilitySettings:
    settings = cast(ObservabilitySettings, request.app.state.observability_settings)
    if not settings.admin_metrics_enabled:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Not found")
    expected = settings.admin_api_token.get_secret_value()
    supplied = supplied_token or ""
    if not hmac.compare_digest(supplied.encode(), expected.encode()):
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Unauthorized")
    return settings


def _release_service(request: Request) -> ReleaseReadinessService:
    engine = cast(AsyncEngine, request.app.state.db_engine)
    settings = cast(Settings, request.app.state.settings)
    return ReleaseReadinessService(create_session_factory(engine), settings)


@router.get("/metrics", response_model=AdminMetrics)
async def metrics(
    request: Request,
    admin_token: Annotated[str | None, Header(alias="X-Admin-Token")] = None,
) -> AdminMetrics:
    _require_admin(request, admin_token)
    service = cast(AdminMetricsService, request.app.state.admin_metrics_service)
    return await service.snapshot()


@router.get("/release-readiness", response_model=ReleaseReadiness)
async def release_readiness(
    request: Request,
    admin_token: Annotated[str | None, Header(alias="X-Admin-Token")] = None,
) -> ReleaseReadiness:
    _require_admin(request, admin_token)
    return await _release_service(request).snapshot()


@router.post("/release-gates/{gate_name}", response_model=ReleaseReadiness)
async def attest_release_gate(
    gate_name: ReleaseGateName,
    body: ReleaseGateAttestationRequest,
    request: Request,
    admin_token: Annotated[str | None, Header(alias="X-Admin-Token")] = None,
) -> ReleaseReadiness:
    _require_admin(request, admin_token)
    try:
        return await _release_service(request).attest(gate_name, body)
    except ReleaseGateError as error:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail={"code": error.code, "blockers": error.blockers},
        ) from error
