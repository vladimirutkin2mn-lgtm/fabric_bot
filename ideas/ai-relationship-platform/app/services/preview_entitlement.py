"""Concurrency-safe one-time free preview entitlement."""

from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.db.models import User


class PreviewOutcome(StrEnum):
    RESERVED = "reserved"
    ALREADY_RESERVED_SAME_ANALYSIS = "already_reserved_same_analysis"
    ALREADY_CONSUMED_SAME_ANALYSIS = "already_consumed_same_analysis"
    UNAVAILABLE = "unavailable"
    NOT_FOUND = "not_found"


@dataclass(frozen=True)
class PreviewState:
    status: str
    analysis_id: UUID | None


class PreviewEntitlementService:
    def __init__(self, sessions: async_sessionmaker[AsyncSession]) -> None:
        self._sessions = sessions

    async def get_preview_state(self, user_id: UUID) -> PreviewState | None:
        async with self._sessions() as session:
            user = await session.get(User, user_id)
            return (
                None
                if user is None
                else PreviewState(user.free_preview_status, user.free_preview_analysis_id)
            )

    async def reserve_preview(self, user_id: UUID, analysis_id: UUID) -> PreviewOutcome:
        async with self._sessions.begin() as session:
            user = await session.scalar(select(User).where(User.id == user_id).with_for_update())
            if user is None:
                return PreviewOutcome.NOT_FOUND
            if user.free_preview_analysis_id == analysis_id:
                return (
                    PreviewOutcome.ALREADY_CONSUMED_SAME_ANALYSIS
                    if user.free_preview_status == "consumed"
                    else PreviewOutcome.ALREADY_RESERVED_SAME_ANALYSIS
                )
            if user.free_preview_status != "available":
                return PreviewOutcome.UNAVAILABLE
            user.free_preview_status, user.free_preview_analysis_id = "reserved", analysis_id
            return PreviewOutcome.RESERVED

    async def consume_preview(self, user_id: UUID, analysis_id: UUID) -> PreviewOutcome:
        return await self._transition(user_id, analysis_id, consume=True)

    async def release_preview(self, user_id: UUID, analysis_id: UUID) -> PreviewOutcome:
        return await self._transition(user_id, analysis_id, consume=False)

    async def _transition(
        self, user_id: UUID, analysis_id: UUID, *, consume: bool
    ) -> PreviewOutcome:
        async with self._sessions.begin() as session:
            user = await session.scalar(select(User).where(User.id == user_id).with_for_update())
            if user is None:
                return PreviewOutcome.NOT_FOUND
            if (
                user.free_preview_analysis_id != analysis_id
                or user.free_preview_status != "reserved"
            ):
                return PreviewOutcome.UNAVAILABLE
            if consume:
                user.free_preview_status, user.free_preview_used_at = "consumed", datetime.now(UTC)
            else:
                user.free_preview_status, user.free_preview_analysis_id = "available", None
            return PreviewOutcome.RESERVED
