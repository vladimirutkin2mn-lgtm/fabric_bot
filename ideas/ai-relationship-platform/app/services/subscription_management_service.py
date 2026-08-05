"""User-owned subscription query, cancel and resume operations."""

from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from typing import cast
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.config import Settings
from app.db.models import Subscription, User
from app.providers.payments.subscription_gateway import SubscriptionGateway
from app.services.subscription_event_processor import SubscriptionEventProcessor

_ACTIVE = ("incomplete", "active", "past_due", "cancel_at_period_end", "paused")


class SubscriptionManagementOutcome(StrEnum):
    UPDATED = "updated"
    NOT_FOUND = "not_found"
    UNAVAILABLE = "unavailable"
    ALREADY_SET = "already_set"


@dataclass(frozen=True)
class SubscriptionView:
    id: UUID
    provider: str
    product_code: str
    status: str
    current_period_end: datetime | None


class SubscriptionManagementService:
    def __init__(
        self,
        sessions: async_sessionmaker[AsyncSession],
        settings: Settings,
        gateways: dict[str, SubscriptionGateway],
        processor: SubscriptionEventProcessor,
    ) -> None:
        self._sessions = sessions
        self._settings = settings
        self._gateways = gateways
        self._processor = processor

    async def current(self, user_id: UUID) -> SubscriptionView | None:
        async with self._sessions() as session:
            value = await session.scalar(
                select(Subscription)
                .where(Subscription.user_id == user_id, Subscription.status.in_(_ACTIVE))
                .order_by(Subscription.created_at.desc())
            )
            return None if value is None else self._view(value)

    async def cancel(self, user_id: UUID, subscription_id: UUID) -> SubscriptionManagementOutcome:
        subscription = await self._owned(user_id, subscription_id)
        if subscription is None:
            return SubscriptionManagementOutcome.NOT_FOUND
        if subscription.status == "cancel_at_period_end":
            return SubscriptionManagementOutcome.ALREADY_SET
        gateway = self._gateways.get(subscription.provider)
        if not self._settings.permits_renewal() or gateway is None:
            return SubscriptionManagementOutcome.UNAVAILABLE
        fact = await gateway.cancel_subscription(subscription.provider_subscription_id)
        await self._processor.apply(fact)
        return SubscriptionManagementOutcome.UPDATED

    async def resume(self, user_id: UUID, subscription_id: UUID) -> SubscriptionManagementOutcome:
        subscription = await self._owned(user_id, subscription_id)
        if subscription is None:
            return SubscriptionManagementOutcome.NOT_FOUND
        if subscription.status != "cancel_at_period_end":
            return SubscriptionManagementOutcome.ALREADY_SET
        gateway = self._gateways.get(subscription.provider)
        if not self._settings.permits_renewal() or gateway is None:
            return SubscriptionManagementOutcome.UNAVAILABLE
        fact = await gateway.resume_subscription(subscription.provider_subscription_id)
        await self._processor.apply(fact)
        return SubscriptionManagementOutcome.UPDATED

    async def _owned(self, user_id: UUID, subscription_id: UUID) -> Subscription | None:
        async with self._sessions() as session:
            user = await session.get(User, user_id)
            if user is None or user.privacy_status != "active":
                return None
            value = await session.scalar(
                select(Subscription).where(
                    Subscription.id == subscription_id,
                    Subscription.user_id == user_id,
                    Subscription.status.in_(_ACTIVE),
                )
            )
            return cast(Subscription | None, value)

    @staticmethod
    def _view(value: Subscription) -> SubscriptionView:
        return SubscriptionView(
            id=value.id,
            provider=value.provider,
            product_code=value.product_code,
            status=value.status,
            current_period_end=value.current_period_end,
        )
