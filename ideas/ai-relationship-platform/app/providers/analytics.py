"""Privacy-preserving product analytics boundary and event contract."""

import logging
import re
from collections.abc import Mapping
from enum import StrEnum
from typing import Protocol
from uuid import UUID

logger = logging.getLogger(__name__)

_SAFE_VALUE = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.:@+-]{0,127}\Z")
_UUID_KEYS = frozenset({"analysis_id", "order_id", "transaction_id", "user_id"})
_INTEGER_KEYS = frozenset(
    {
        "amount_minor",
        "attempt_count",
        "character_count_bucket",
        "credits",
        "message_count_bucket",
        "score",
    }
)


class EventScope(StrEnum):
    """Durable identity used to suppress duplicate transition events."""

    USER = "user"
    ANALYSIS = "analysis"
    ORDER = "order"
    ACCOUNT = "account"
    ACTION = "action"


_EVENT_PROPERTIES: dict[str, frozenset[str]] = {
    "bot_started": frozenset(),
    "main_menu_opened": frozenset(),
    "age_confirmed": frozenset(),
    "consent_accepted": frozenset({"consent_version"}),
    "onboarding_completed": frozenset({"consent_version"}),
    "analysis_started": frozenset({"analysis_id", "source_type"}),
    "conversation_submitted": frozenset(
        {
            "analysis_id",
            "source_type",
            "source_format",
            "message_count_bucket",
            "character_count_bucket",
        }
    ),
    "conversation_parsed": frozenset(
        {
            "analysis_id",
            "source_type",
            "source_format",
            "message_count_bucket",
            "character_count_bucket",
        }
    ),
    "conversation_rejected": frozenset({"analysis_id", "rejection_reason"}),
    "analysis_context_completed": frozenset({"analysis_id", "relationship_stage_code"}),
    "analysis_cancelled": frozenset({"analysis_id"}),
    "preview_viewed": frozenset({"analysis_id"}),
    "paywall_viewed": frozenset({"analysis_id"}),
    "credit_spent": frozenset({"analysis_id", "transaction_id", "credits"}),
    "credit_refunded": frozenset({"analysis_id", "transaction_id", "credits"}),
    "checkout_started": frozenset(
        {"order_id", "product_code", "provider", "market", "currency", "credits"}
    ),
    "purchase_completed": frozenset(
        {"order_id", "product_code", "provider", "market", "currency", "credits"}
    ),
    "payment_failed": frozenset(
        {"order_id", "product_code", "provider", "market", "currency", "failure_code"}
    ),
    "analysis_processing_started": frozenset(
        {"analysis_id", "provider", "model", "prompt_version"}
    ),
    "analysis_completed": frozenset(
        {
            "analysis_id",
            "provider",
            "model",
            "prompt_version",
            "attempt_count",
            "repair_used",
            "latency_bucket",
            "input_token_bucket",
            "output_token_bucket",
        }
    ),
    "analysis_failed": frozenset(
        {
            "analysis_id",
            "provider",
            "model",
            "prompt_version",
            "attempt_count",
            "repair_used",
            "latency_bucket",
            "input_token_bucket",
            "output_token_bucket",
            "failure_code",
        }
    ),
    "analysis_feedback_submitted": frozenset({"analysis_id", "score"}),
    "reply_suggestions_requested": frozenset({"analysis_id"}),
    "followup_requested": frozenset({"analysis_id"}),
    "analysis_deleted": frozenset({"analysis_id"}),
    "all_data_deleted": frozenset({"user_id"}),
}

_EVENT_SCOPES: dict[str, EventScope] = {
    "bot_started": EventScope.USER,
    "age_confirmed": EventScope.USER,
    "consent_accepted": EventScope.USER,
    "onboarding_completed": EventScope.USER,
    "analysis_started": EventScope.ANALYSIS,
    "conversation_submitted": EventScope.ANALYSIS,
    "conversation_parsed": EventScope.ANALYSIS,
    "analysis_context_completed": EventScope.ANALYSIS,
    "analysis_cancelled": EventScope.ANALYSIS,
    "preview_viewed": EventScope.ANALYSIS,
    "paywall_viewed": EventScope.ANALYSIS,
    "credit_spent": EventScope.ANALYSIS,
    "credit_refunded": EventScope.ANALYSIS,
    "analysis_processing_started": EventScope.ANALYSIS,
    "analysis_completed": EventScope.ANALYSIS,
    "analysis_failed": EventScope.ANALYSIS,
    "analysis_feedback_submitted": EventScope.ANALYSIS,
    "analysis_deleted": EventScope.ANALYSIS,
    "checkout_started": EventScope.ORDER,
    "purchase_completed": EventScope.ORDER,
    "payment_failed": EventScope.ORDER,
    "all_data_deleted": EventScope.ACCOUNT,
    "main_menu_opened": EventScope.ACTION,
    "conversation_rejected": EventScope.ACTION,
    "reply_suggestions_requested": EventScope.ACTION,
    "followup_requested": EventScope.ACTION,
}


class AnalyticsContractError(ValueError):
    """A generic error that never echoes rejected names or values."""

    def __init__(self) -> None:
        super().__init__("analytics event violates the safe contract")


class AnalyticsClient(Protocol):
    """Track allow-listed lifecycle data, never user message content."""

    async def track(
        self, user_id: str | None, event: str, properties: Mapping[str, str] | None = None
    ) -> None: ...


class NoOpAnalyticsClient:
    """Disabled analytics implementation."""

    async def track(
        self, user_id: str | None, event: str, properties: Mapping[str, str] | None = None
    ) -> None:
        return None


class DiscardingAnalyticsClient:
    """Explicit sink used only when analytics is intentionally disabled."""

    async def track(
        self, user_id: str | None, event: str, properties: Mapping[str, str] | None = None
    ) -> None:
        logger.info("analytics_event_intentionally_discarded event=%s", event)


class ResilientAnalyticsClient:
    """Keep analytics failures outside already committed business transitions."""

    def __init__(self, inner: AnalyticsClient) -> None:
        self._inner = inner

    async def track(
        self, user_id: str | None, event: str, properties: Mapping[str, str] | None = None
    ) -> None:
        try:
            await self._inner.track(user_id, event, properties)
        except Exception:
            logger.warning("analytics_delivery_failed event=%s", _safe_event_for_log(event))


def validate_event_properties(event: str, properties: Mapping[str, str] | None) -> dict[str, str]:
    """Return a validated copy containing only short, structured metadata."""
    allowed = _EVENT_PROPERTIES.get(event)
    if allowed is None:
        raise AnalyticsContractError
    supplied = dict(properties or {})
    if not supplied.keys() <= allowed:
        raise AnalyticsContractError
    for key, value in supplied.items():
        if not isinstance(value, str) or not value or len(value) > 128 or not value.isprintable():
            raise AnalyticsContractError
        if key in _UUID_KEYS:
            try:
                UUID(value)
            except ValueError:
                raise AnalyticsContractError from None
        elif key in _INTEGER_KEYS:
            try:
                int(value)
            except ValueError:
                raise AnalyticsContractError from None
        elif _SAFE_VALUE.fullmatch(value) is None:
            raise AnalyticsContractError
    return supplied


def event_scope(event: str) -> EventScope:
    """Return the configured idempotency scope for a known event."""
    try:
        return _EVENT_SCOPES[event]
    except KeyError:
        raise AnalyticsContractError from None


def event_identity(
    user_id: str | None,
    event: str,
    properties: Mapping[str, str],
    correlation_id: str,
) -> tuple[str | None, str]:
    """Return pseudonymous subject and deterministic transition idempotency key."""
    scope = event_scope(event)
    subject = _validated_optional_uuid(user_id)
    if scope is EventScope.USER:
        identity = subject
    elif scope is EventScope.ANALYSIS:
        identity = properties.get("analysis_id")
    elif scope is EventScope.ORDER:
        identity = properties.get("order_id")
    elif scope is EventScope.ACCOUNT:
        identity = properties.get("user_id")
    else:
        identity = correlation_id
    if identity is None:
        raise AnalyticsContractError
    return subject, f"{event}:{identity}"


def known_event_names() -> tuple[str, ...]:
    """Expose a stable ordered list for admin funnel aggregation and tests."""
    return tuple(_EVENT_PROPERTIES)


def _validated_optional_uuid(value: str | None) -> str | None:
    if value is None:
        return None
    try:
        return str(UUID(value))
    except ValueError:
        raise AnalyticsContractError from None


def _safe_event_for_log(event: str) -> str:
    return event if event in _EVENT_PROPERTIES else "unknown"
