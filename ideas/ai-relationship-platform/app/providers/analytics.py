"""Privacy-preserving product analytics boundary."""

from collections.abc import Mapping
from typing import Protocol


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
