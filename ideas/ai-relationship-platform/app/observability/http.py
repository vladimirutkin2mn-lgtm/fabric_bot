"""ASGI correlation and unexpected-error middleware."""

from collections.abc import Awaitable, Callable, MutableSequence
from typing import Any

from starlette.types import ASGIApp, Message, Receive, Scope, Send

from app.observability.context import reset_correlation_id, set_correlation_id
from app.observability.errors import ErrorReporter, report_unexpected

_HEADER = b"x-correlation-id"


class HttpObservabilityMiddleware:
    """Propagate a bounded correlation ID and classify unexpected failures."""

    def __init__(self, app: ASGIApp, reporter: ErrorReporter) -> None:
        self.app = app
        self.reporter = reporter

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        incoming = _header_value(scope.get("headers", []), _HEADER)
        correlation_id, token = set_correlation_id(incoming)

        async def send_with_correlation(message: Message) -> None:
            if message["type"] == "http.response.start":
                headers: MutableSequence[tuple[bytes, bytes]] = message.setdefault("headers", [])
                headers[:] = [(key, value) for key, value in headers if key.lower() != _HEADER]
                headers.append((_HEADER, correlation_id.encode("ascii")))
            await send(message)

        try:
            await self.app(scope, receive, send_with_correlation)
        except Exception as error:
            report_unexpected(
                self.reporter,
                error,
                surface="http",
                operation=_safe_operation(scope),
            )
            raise
        finally:
            reset_correlation_id(token)


def _header_value(headers: list[tuple[bytes, bytes]], name: bytes) -> str | None:
    for key, value in headers:
        if key.lower() == name:
            try:
                return value.decode("ascii")
            except UnicodeDecodeError:
                return None
    return None


def _safe_operation(scope: Scope) -> str:
    route = scope.get("route")
    template = getattr(route, "path", None)
    method = str(scope.get("method", "unknown")).lower()
    if isinstance(template, str) and template:
        cleaned = template.strip("/").replace("/", ".").replace("{", "").replace("}", "")
        return f"{method}.{cleaned or 'root'}"
    return f"{method}.unmatched"
