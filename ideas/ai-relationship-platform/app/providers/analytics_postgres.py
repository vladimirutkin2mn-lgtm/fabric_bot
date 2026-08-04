"""Idempotent PostgreSQL implementation of the analytics boundary."""

from collections.abc import Mapping

from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.db.analytics import AnalyticsEvent
from app.observability.context import correlation_id_for_event
from app.observability.settings import ObservabilitySettings
from app.providers.analytics import (
    AnalyticsClient,
    NoOpAnalyticsClient,
    ResilientAnalyticsClient,
    event_identity,
    validate_event_properties,
)


class PostgresAnalyticsClient:
    """Store only allow-listed metadata and suppress duplicate transitions."""

    def __init__(self, sessions: async_sessionmaker[AsyncSession]) -> None:
        self._sessions = sessions

    async def track(
        self, user_id: str | None, event: str, properties: Mapping[str, str] | None = None
    ) -> None:
        safe_properties = validate_event_properties(event, properties)
        correlation_id = correlation_id_for_event()
        subject_id, idempotency_key = event_identity(
            user_id, event, safe_properties, correlation_id
        )
        async with self._sessions.begin() as session:
            await session.execute(
                insert(AnalyticsEvent)
                .values(
                    event_name=event,
                    subject_id=subject_id,
                    properties=safe_properties,
                    idempotency_key=idempotency_key,
                    correlation_id=correlation_id,
                )
                .on_conflict_do_nothing(index_elements=[AnalyticsEvent.idempotency_key])
            )


def create_analytics_client(
    sessions: async_sessionmaker[AsyncSession], settings: ObservabilitySettings
) -> AnalyticsClient:
    """Compose one analytics provider for API and bot processes."""
    if settings.analytics_backend == "postgres":
        return ResilientAnalyticsClient(PostgresAnalyticsClient(sessions))
    return NoOpAnalyticsClient()
