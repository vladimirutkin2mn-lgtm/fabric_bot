"""Privacy-preserving product analytics boundary."""

import logging
from collections.abc import Mapping
from typing import Protocol

logger = logging.getLogger(__name__)


class AnalyticsClient(Protocol):
    """Track allow-listed lifecycle data, never user message content."""

    async def track(
        self, user_id: str | None, event: str, properties: Mapping[str, str] | None = None
    ) -> None: ...


class NoOpAnalyticsClient:
    """Default analytics implementation for local and production bootstrap."""

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
