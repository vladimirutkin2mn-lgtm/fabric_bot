"""Sentry-compatible error boundary without an external SDK dependency."""

import logging
import re
from collections.abc import Mapping
from typing import Protocol

from app.observability.context import current_correlation_id

logger = logging.getLogger(__name__)
_SAFE_NAME = re.compile(r"[A-Za-z_][A-Za-z0-9_.:-]{0,127}\Z")
_ALLOWED_CONTEXT = frozenset({"surface", "operation"})


class ErrorReporter(Protocol):
    """Minimal boundary that a future Sentry adapter can implement."""

    def capture_exception(
        self,
        error: BaseException,
        *,
        context: Mapping[str, str] | None = None,
        correlation_id: str | None = None,
    ) -> None: ...


class NoOpErrorReporter:
    """Disabled error reporting."""

    def capture_exception(
        self,
        error: BaseException,
        *,
        context: Mapping[str, str] | None = None,
        correlation_id: str | None = None,
    ) -> None:
        return None


class LoggingErrorReporter:
    """Log only safe classification metadata, never exception messages or payloads."""

    def capture_exception(
        self,
        error: BaseException,
        *,
        context: Mapping[str, str] | None = None,
        correlation_id: str | None = None,
    ) -> None:
        safe_context = _safe_context(context)
        logger.error(
            "unexpected_error surface=%s operation=%s exception_type=%s reported_correlation_id=%s",
            safe_context.get("surface", "unknown"),
            safe_context.get("operation", "unknown"),
            _safe_name(type(error).__name__),
            correlation_id or current_correlation_id(),
        )


def report_unexpected(
    reporter: ErrorReporter,
    error: BaseException,
    *,
    surface: str,
    operation: str,
) -> None:
    """Keep failures in the reporter outside the application correctness path."""
    try:
        reporter.capture_exception(
            error,
            context={"surface": _safe_name(surface), "operation": _safe_name(operation)},
            correlation_id=current_correlation_id(),
        )
    except Exception:
        logger.error(
            "error_reporter_failed surface=%s operation=%s exception_type=%s",
            _safe_name(surface),
            _safe_name(operation),
            _safe_name(type(error).__name__),
        )


def _safe_context(context: Mapping[str, str] | None) -> dict[str, str]:
    if context is None:
        return {}
    return {
        key: _safe_name(value)
        for key, value in context.items()
        if key in _ALLOWED_CONTEXT
    }


def _safe_name(value: str) -> str:
    candidate = value.strip()
    return candidate if _SAFE_NAME.fullmatch(candidate) else "unknown"
