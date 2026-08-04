"""Unit coverage for privacy-safe observability boundaries."""

import logging
from collections.abc import Mapping
from typing import Any
from uuid import uuid4

import pytest
from aiogram.types import Update

from app.bot.observability import TelegramObservabilityMiddleware
from app.logging import CorrelationIdFilter
from app.observability.context import (
    current_correlation_id,
    normalize_correlation_id,
    reset_correlation_id,
    set_correlation_id,
)
from app.observability.errors import LoggingErrorReporter, report_unexpected
from app.providers.analytics import (
    AnalyticsContractError,
    event_identity,
    validate_event_properties,
)


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


def test_correlation_id_accepts_bounded_safe_value_and_rejects_private_text() -> None:
    assert normalize_correlation_id("request-123") == "request-123"
    generated = normalize_correlation_id("private message with spaces " * 10)
    assert len(generated) == 32
    assert generated.isalnum()
    assert "private" not in generated


def test_context_is_reset_after_scope() -> None:
    correlation_id, token = set_correlation_id("request-456")
    assert correlation_id == "request-456"
    assert current_correlation_id() == "request-456"
    reset_correlation_id(token)
    assert current_correlation_id() == "-"


def test_analytics_contract_rejects_unknown_or_private_properties_without_echo() -> None:
    sentinel = "private-conversation-sentinel"
    for event, properties in (
        (sentinel, {}),
        ("analysis_completed", {"report_text": sentinel}),
        ("analysis_completed", {"analysis_id": sentinel}),
    ):
        with pytest.raises(AnalyticsContractError) as raised:
            validate_event_properties(event, properties)
        assert sentinel not in str(raised.value)
        assert sentinel not in repr(raised.value)


def test_transition_and_action_identities_are_stable_and_separate() -> None:
    user_id = str(uuid4())
    analysis_id = str(uuid4())
    subject, transition_key = event_identity(
        user_id,
        "analysis_completed",
        {"analysis_id": analysis_id},
        "correlation-a",
    )
    _, action_key = event_identity(
        user_id,
        "reply_suggestions_requested",
        {"analysis_id": analysis_id},
        "correlation-b",
    )
    assert subject == user_id
    assert transition_key == f"analysis_completed:{analysis_id}"
    assert action_key == "reply_suggestions_requested:correlation-b"


async def test_telegram_middleware_uses_update_id_not_telegram_identity() -> None:
    reporter = RecordingReporter()
    middleware = TelegramObservabilityMiddleware(reporter)
    observed: list[str] = []

    async def handler(event: Update, data: dict[str, Any]) -> str:
        observed.append(current_correlation_id())
        return "ok"

    result = await middleware(handler, Update(update_id=987654), {})
    assert result == "ok"
    assert observed == ["tg-update-987654"]
    assert current_correlation_id() == "-"
    assert reporter.calls == []


async def test_telegram_middleware_reports_only_safe_failure_metadata() -> None:
    sentinel = "private-message-sentinel"
    reporter = RecordingReporter()
    middleware = TelegramObservabilityMiddleware(reporter)

    async def handler(event: Update, data: dict[str, Any]) -> None:
        raise RuntimeError(sentinel)

    with pytest.raises(RuntimeError):
        await middleware(handler, Update(update_id=42), {})
    assert reporter.calls == [
        (
            "RuntimeError",
            {"surface": "telegram", "operation": "Update"},
            "tg-update-42",
        )
    ]
    assert sentinel not in repr(reporter.calls)


def test_error_reporter_logs_exception_class_but_not_message(
    caplog: pytest.LogCaptureFixture,
) -> None:
    sentinel = "private-report-sentinel"
    _, token = set_correlation_id("request-safe")
    try:
        with caplog.at_level(logging.ERROR):
            report_unexpected(
                LoggingErrorReporter(),
                RuntimeError(sentinel),
                surface="http",
                operation="get.admin.metrics",
            )
    finally:
        reset_correlation_id(token)
    text = caplog.text
    assert "RuntimeError" in text
    assert "request-safe" in text
    assert sentinel not in text


def test_logging_filter_adds_safe_placeholder() -> None:
    record = logging.LogRecord("test", logging.INFO, __file__, 1, "hello", (), None)
    assert CorrelationIdFilter().filter(record)
    assert record.correlation_id == "-"
