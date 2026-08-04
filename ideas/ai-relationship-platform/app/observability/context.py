"""Bounded correlation IDs shared by HTTP, Telegram, logs and analytics."""

import re
from contextvars import ContextVar, Token
from uuid import uuid4

_CORRELATION_ID: ContextVar[str | None] = ContextVar("correlation_id", default=None)
_SAFE_CORRELATION_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,63}\Z")


def normalize_correlation_id(value: str | None) -> str:
    """Accept a short safe identifier or generate a new opaque value."""
    candidate = (value or "").strip()
    if _SAFE_CORRELATION_ID.fullmatch(candidate):
        return candidate
    return uuid4().hex


def set_correlation_id(value: str | None = None) -> tuple[str, Token[str | None]]:
    """Set a safe identifier and return the value plus reset token."""
    correlation_id = normalize_correlation_id(value)
    return correlation_id, _CORRELATION_ID.set(correlation_id)


def reset_correlation_id(token: Token[str | None]) -> None:
    """Restore the previous context after a request or update."""
    _CORRELATION_ID.reset(token)


def current_correlation_id() -> str:
    """Return the active identifier or a log-safe placeholder."""
    return _CORRELATION_ID.get() or "-"


def correlation_id_for_event() -> str:
    """Return an active ID or a one-shot ID without mutating ambient context."""
    value = _CORRELATION_ID.get()
    return value if value is not None else uuid4().hex
