"""Durable webhook inbox; raw provider payloads are deliberately never stored."""

import logging

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.db.models import BillingJob, ProviderWebhookEvent

logger = logging.getLogger(__name__)


class WebhookInboxService:
    def __init__(self, sessions: async_sessionmaker[AsyncSession]) -> None:
        self._sessions = sessions

    async def accept(
        self, provider: str, event_id: str, event_type: str, object_id: str, payload_hash: str
    ) -> ProviderWebhookEvent:
        async with self._sessions.begin() as session:
            existing = await session.scalar(
                select(ProviderWebhookEvent)
                .where(
                    ProviderWebhookEvent.provider == provider,
                    ProviderWebhookEvent.provider_event_id == event_id,
                )
                .with_for_update()
            )
            if existing:
                if existing.payload_hash != payload_hash:
                    existing.status = "manual_review"
                    existing.last_error_code = "duplicate_payload_mismatch"
                    logger.warning(
                        "webhook_duplicate_payload_mismatch provider=%s event_id=%s",
                        provider,
                        event_id,
                    )
                return existing
            event = ProviderWebhookEvent(
                provider=provider,
                provider_event_id=event_id,
                event_type=event_type,
                provider_object_id=object_id,
                payload_hash=payload_hash,
            )
            session.add(event)
            await session.flush()
            session.add(
                BillingJob(
                    job_type="webhook_processing",
                    provider=provider,
                    object_type="webhook_event",
                    object_id=str(event.id),
                    idempotency_key=f"webhook:{provider}:{event_id}",
                )
            )
            return event
