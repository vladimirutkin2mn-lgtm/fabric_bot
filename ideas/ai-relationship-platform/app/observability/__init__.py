"""Shared privacy-safe observability boundaries."""

from app.observability.context import (
    correlation_id_for_event,
    current_correlation_id,
    normalize_correlation_id,
    reset_correlation_id,
    set_correlation_id,
)
from app.observability.errors import (
    ErrorReporter,
    LoggingErrorReporter,
    NoOpErrorReporter,
    report_unexpected,
)

__all__ = [
    "ErrorReporter",
    "LoggingErrorReporter",
    "NoOpErrorReporter",
    "correlation_id_for_event",
    "current_correlation_id",
    "normalize_correlation_id",
    "report_unexpected",
    "reset_correlation_id",
    "set_correlation_id",
]
