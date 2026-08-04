"""HTTP correlation and error-reporting acceptance tests."""

from collections.abc import Mapping

import pytest
from httpx import ASGITransport, AsyncClient
from starlette.types import Message, Receive, Scope, Send

from app.observability.context import current_correlation_id
from app.observability.http import HttpObservabilityMiddleware


class RecordingReporter:
    def __init__(self) -> None:
        self.calls: list[tuple[str, dict[str, str], str | None]] = []

    def capture_exception(
        self,
        error: BaseException,
        *,
        context: Mapping[str, str] | None = None,
        correlation_id: str | None = None,
    ) -> None:
        self.calls.append((type(error).__name__, dict(context or {}), correlation_id))


async def test_http_middleware_reports_only_safe_context_and_resets_scope() -> None:
    sentinel = "private-http-body-sentinel"
    reporter = RecordingReporter()

    async def failing_app(scope: Scope, receive: Receive, send: Send) -> None:
        raise RuntimeError(sentinel)

    app = HttpObservabilityMiddleware(failing_app, reporter)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        with pytest.raises(RuntimeError):
            await client.get(
                "/private/path",
                headers={"X-Correlation-ID": "http-error-1"},
            )

    assert reporter.calls == [
        (
            "RuntimeError",
            {"surface": "http", "operation": "get.unmatched"},
            "http-error-1",
        )
    ]
    assert sentinel not in repr(reporter.calls)
    assert current_correlation_id() == "-"


async def test_http_middleware_returns_correlation_header() -> None:
    reporter = RecordingReporter()

    async def successful_app(scope: Scope, receive: Receive, send: Send) -> None:
        assert current_correlation_id() == "http-success-1"
        await send(
            {
                "type": "http.response.start",
                "status": 204,
                "headers": [(b"x-correlation-id", b"must-be-replaced")],
            }
        )
        await send({"type": "http.response.body", "body": b""})

    app = HttpObservabilityMiddleware(successful_app, reporter)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.get(
            "/health",
            headers={"X-Correlation-ID": "http-success-1"},
        )

    assert response.status_code == 204
    assert response.headers.get_list("x-correlation-id") == ["http-success-1"]
    assert reporter.calls == []
    assert current_correlation_id() == "-"
