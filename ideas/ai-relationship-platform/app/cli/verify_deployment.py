"""Verify a deployed API and its Telegram webhook without exposing secrets."""

import argparse
import asyncio
import json
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from typing import cast
from urllib.parse import urlsplit

import httpx
from pydantic import ValidationError

from app.config import Settings, get_settings
from app.deployment import validate_telegram_webhook


class VerificationConfigurationError(ValueError):
    """Safe deployment verification configuration failure."""


@dataclass(frozen=True)
class VerificationCheck:
    """One safe, user-visible release verification result."""

    name: str
    passed: bool
    detail: str


def api_origin(webhook_url: str) -> str:
    """Return the HTTPS origin for an already validated Telegram webhook URL."""
    parsed = urlsplit(webhook_url)
    if (
        parsed.scheme not in {"http", "https"}
        or not parsed.netloc
        or parsed.username is not None
        or parsed.password is not None
    ):
        raise VerificationConfigurationError("Telegram webhook origin is invalid")
    return f"{parsed.scheme}://{parsed.netloc}"


def _json_object(response: httpx.Response) -> dict[str, object] | None:
    try:
        value: object = response.json()
    except ValueError:
        return None
    if not isinstance(value, dict) or not all(isinstance(key, str) for key in value):
        return None
    return cast(dict[str, object], value)


class DeploymentVerifier:
    """Run bounded, privacy-safe checks against one deployed HeartSignal instance."""

    def __init__(
        self,
        settings: Settings,
        client: httpx.AsyncClient,
        *,
        now: datetime | None = None,
        max_pending_updates: int = 100,
        recent_error_seconds: int = 900,
    ) -> None:
        if max_pending_updates < 0 or recent_error_seconds <= 0:
            raise ValueError("verification bounds are invalid")
        validate_telegram_webhook(settings)
        if not settings.webhook_enabled:
            raise VerificationConfigurationError("Telegram webhook mode is not enabled")
        self._settings = settings
        self._client = client
        self._origin = api_origin(settings.telegram_webhook_url)
        self._now = now or datetime.now(UTC)
        self._max_pending_updates = max_pending_updates
        self._recent_error_seconds = recent_error_seconds

    async def verify(self) -> tuple[VerificationCheck, ...]:
        """Return every check; network and payload failures remain safe result values."""
        checks = [
            await self._check_liveness(),
            await self._check_readiness(),
            await self._check_webhook_authentication(),
        ]
        checks.extend(await self._check_telegram_webhook())
        return tuple(checks)

    async def _request(
        self,
        method: str,
        url: str,
        **kwargs: object,
    ) -> httpx.Response | None:
        try:
            return await self._client.request(method, url, **kwargs)
        except httpx.HTTPError:
            return None

    async def _check_liveness(self) -> VerificationCheck:
        response = await self._request("GET", f"{self._origin}/health/live")
        payload = None if response is None else _json_object(response)
        passed = bool(
            response is not None
            and response.status_code == 200
            and payload is not None
            and payload.get("status") == "ok"
        )
        return VerificationCheck(
            "api_liveness",
            passed,
            "API process is responsive" if passed else "liveness check failed",
        )

    async def _check_readiness(self) -> VerificationCheck:
        response = await self._request("GET", f"{self._origin}/health/ready")
        payload = None if response is None else _json_object(response)
        passed = bool(
            response is not None
            and response.status_code == 200
            and payload is not None
            and payload.get("status") == "ok"
            and payload.get("database") == "available"
            and payload.get("schema") == "current"
        )
        return VerificationCheck(
            "api_readiness",
            passed,
            "database and schema are ready" if passed else "readiness check failed",
        )

    async def _check_webhook_authentication(self) -> VerificationCheck:
        probe_secret = "invalid-release-verification-secret"
        if probe_secret == self._settings.telegram_webhook_secret.get_secret_value():
            probe_secret += "-x"
        response = await self._request(
            "POST",
            self._settings.telegram_webhook_url,
            content=b"{}",
            headers={"X-Telegram-Bot-Api-Secret-Token": probe_secret},
        )
        passed = response is not None and response.status_code == 401
        return VerificationCheck(
            "telegram_webhook_authentication",
            passed,
            "wrong webhook secret is rejected"
            if passed
            else "webhook authentication check failed",
        )

    async def _check_telegram_webhook(self) -> tuple[VerificationCheck, ...]:
        token = self._settings.telegram_bot_token.get_secret_value()
        response = await self._request(
            "GET",
            f"https://api.telegram.org/bot{token}/getWebhookInfo",
        )
        payload = None if response is None else _json_object(response)
        result_object = None if payload is None else payload.get("result")
        if (
            response is None
            or response.status_code != 200
            or payload is None
            or payload.get("ok") is not True
            or not isinstance(result_object, dict)
            or not all(isinstance(key, str) for key in result_object)
        ):
            return (
                VerificationCheck(
                    "telegram_webhook_configuration", False, "Telegram webhook check failed"
                ),
                VerificationCheck(
                    "telegram_update_backlog", False, "Telegram backlog was not checked"
                ),
                VerificationCheck(
                    "telegram_delivery_errors", False, "Telegram delivery errors were not checked"
                ),
            )

        result = cast(dict[str, object], result_object)
        configured_url = result.get("url")
        allowed = result.get("allowed_updates")
        allowed_set = (
            {item for item in allowed if isinstance(item, str)}
            if isinstance(allowed, list)
            else set()
        )
        allowed_valid = not allowed_set or {"message", "callback_query"}.issubset(allowed_set)
        configuration_passed = (
            configured_url == self._settings.telegram_webhook_url and allowed_valid
        )

        pending_value = result.get("pending_update_count")
        pending = (
            pending_value
            if isinstance(pending_value, int) and not isinstance(pending_value, bool)
            else None
        )
        backlog_passed = pending is not None and pending <= self._max_pending_updates

        error_value = result.get("last_error_date")
        last_error_date = (
            error_value if isinstance(error_value, int) and not isinstance(error_value, bool) else None
        )
        recent_error = bool(
            last_error_date is not None
            and self._now.timestamp() - last_error_date <= self._recent_error_seconds
        )

        return (
            VerificationCheck(
                "telegram_webhook_configuration",
                configuration_passed,
                "Telegram webhook URL and allowed updates match"
                if configuration_passed
                else "Telegram webhook configuration does not match",
            ),
            VerificationCheck(
                "telegram_update_backlog",
                backlog_passed,
                f"{pending} pending Telegram updates"
                if pending is not None
                else "Telegram backlog payload is invalid",
            ),
            VerificationCheck(
                "telegram_delivery_errors",
                not recent_error,
                "no recent Telegram delivery error"
                if not recent_error
                else "Telegram reported a recent delivery error",
            ),
        )


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Verify a deployed HeartSignal release")
    parser.add_argument("--timeout-seconds", type=float, default=10.0)
    parser.add_argument("--max-pending-updates", type=int, default=100)
    parser.add_argument("--recent-error-seconds", type=int, default=900)
    parser.add_argument("--json", action="store_true", dest="json_output")
    return parser


async def run(args: argparse.Namespace) -> int:
    """Load production configuration, execute checks, and return a shell exit code."""
    try:
        settings = get_settings()
        if settings.app_env not in {"staging", "production"}:
            raise VerificationConfigurationError(
                "deployment verification requires staging or production APP_ENV"
            )
        if args.timeout_seconds <= 0:
            raise VerificationConfigurationError("timeout must be positive")
        async with httpx.AsyncClient(
            timeout=args.timeout_seconds,
            follow_redirects=False,
        ) as client:
            checks = await DeploymentVerifier(
                settings,
                client,
                max_pending_updates=args.max_pending_updates,
                recent_error_seconds=args.recent_error_seconds,
            ).verify()
    except (ValidationError, ValueError, VerificationConfigurationError):
        print("[FAIL] configuration: deployment verification configuration is invalid")
        return 2

    if args.json_output:
        print(json.dumps([asdict(check) for check in checks], ensure_ascii=True))
    else:
        for check in checks:
            marker = "PASS" if check.passed else "FAIL"
            print(f"[{marker}] {check.name}: {check.detail}")
    return 0 if all(check.passed for check in checks) else 1


def main() -> None:
    raise SystemExit(asyncio.run(run(_parser().parse_args())))


if __name__ == "__main__":
    main()
