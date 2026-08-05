"""Post-deploy verification tests with fully mocked network transport."""

from datetime import UTC, datetime

import httpx
import pytest
from pydantic import SecretStr

from app.cli.verify_deployment import (
    DeploymentVerifier,
    VerificationConfigurationError,
    api_origin,
)
from app.config import Settings

_TOKEN = "123456789:AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA"
_WEBHOOK = "https://heartsignal.example/telegram/webhook"
_NOW = datetime(2026, 8, 5, 4, 30, tzinfo=UTC)


def production_settings() -> Settings:
    return Settings(
        app_env="production",
        database_url="postgresql+asyncpg://user:pass@db:5432/heartsignal",
        telegram_bot_token=SecretStr(_TOKEN),
        telegram_webhook_url=_WEBHOOK,
        telegram_webhook_secret=SecretStr("release-verification-secret-0123456789"),
        content_encryption_key=SecretStr(
            "0123456789abcdefghijklmnopqrstuvwxyz-HEARTSIGNAL-production-key"
        ),
        payment_public_base_url="https://heartsignal.example",
    )


def mock_transport(
    *,
    ready_status: int = 200,
    ready_payload: object | None = None,
    webhook_status: int = 401,
    telegram_result: dict[str, object] | None = None,
    telegram_error: bool = False,
) -> httpx.MockTransport:
    readiness = ready_payload or {
        "status": "ok",
        "database": "available",
        "schema": "current",
    }
    telegram = telegram_result or {
        "url": _WEBHOOK,
        "allowed_updates": ["message", "callback_query"],
        "pending_update_count": 0,
    }

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.host == "heartsignal.example":
            if request.url.path == "/health/live":
                return httpx.Response(200, json={"status": "ok"})
            if request.url.path == "/health/ready":
                return httpx.Response(ready_status, json=readiness)
            if request.url.path == "/telegram/webhook":
                assert request.headers["X-Telegram-Bot-Api-Secret-Token"] != (
                    production_settings().telegram_webhook_secret.get_secret_value()
                )
                return httpx.Response(webhook_status, json={"detail": "invalid"})
        if request.url.host == "api.telegram.org":
            if telegram_error:
                raise httpx.ConnectError("offline", request=request)
            assert request.url.path == f"/bot{_TOKEN}/getWebhookInfo"
            return httpx.Response(200, json={"ok": True, "result": telegram})
        raise AssertionError(f"unexpected request host: {request.url.host}")

    return httpx.MockTransport(handler)


@pytest.mark.asyncio
async def test_verifier_passes_complete_release_contract() -> None:
    async with httpx.AsyncClient(transport=mock_transport()) as client:
        checks = await DeploymentVerifier(production_settings(), client, now=_NOW).verify()

    assert len(checks) == 6
    assert all(check.passed for check in checks)
    assert {check.name for check in checks} == {
        "api_liveness",
        "api_readiness",
        "telegram_webhook_authentication",
        "telegram_webhook_configuration",
        "telegram_update_backlog",
        "telegram_delivery_errors",
    }


@pytest.mark.asyncio
async def test_verifier_fails_stale_schema_wrong_webhook_backlog_and_recent_error() -> None:
    recent_error = int(_NOW.timestamp()) - 60
    async with httpx.AsyncClient(
        transport=mock_transport(
            ready_status=503,
            ready_payload={"detail": "database schema is not current"},
            telegram_result={
                "url": "https://wrong.example/telegram/webhook",
                "allowed_updates": ["message"],
                "pending_update_count": 101,
                "last_error_date": recent_error,
                "last_error_message": "private provider detail must not be emitted",
            },
        )
    ) as client:
        checks = await DeploymentVerifier(
            production_settings(),
            client,
            now=_NOW,
            max_pending_updates=100,
            recent_error_seconds=900,
        ).verify()

    by_name = {check.name: check for check in checks}
    assert not by_name["api_readiness"].passed
    assert not by_name["telegram_webhook_configuration"].passed
    assert not by_name["telegram_update_backlog"].passed
    assert not by_name["telegram_delivery_errors"].passed
    assert all("private provider detail" not in check.detail for check in checks)


@pytest.mark.asyncio
async def test_verifier_fails_closed_without_leaking_bot_token_on_network_error() -> None:
    async with httpx.AsyncClient(transport=mock_transport(telegram_error=True)) as client:
        checks = await DeploymentVerifier(production_settings(), client, now=_NOW).verify()

    telegram_checks = [check for check in checks if check.name.startswith("telegram_")]
    assert not all(check.passed for check in telegram_checks)
    assert all(_TOKEN not in check.detail for check in checks)


@pytest.mark.asyncio
async def test_verifier_requires_webhook_route_to_reject_wrong_secret() -> None:
    async with httpx.AsyncClient(transport=mock_transport(webhook_status=204)) as client:
        checks = await DeploymentVerifier(production_settings(), client, now=_NOW).verify()

    authentication = next(
        check for check in checks if check.name == "telegram_webhook_authentication"
    )
    assert not authentication.passed


def test_api_origin_rejects_embedded_credentials() -> None:
    with pytest.raises(VerificationConfigurationError):
        api_origin("https://user:password@heartsignal.example/telegram/webhook")
