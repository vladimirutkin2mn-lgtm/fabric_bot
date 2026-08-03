"""Analysis persistence boundary."""

from typing import Protocol, cast
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import Analysis


class AnalysisRepository(Protocol):
    async def create_or_resume(self, user_id: UUID) -> tuple[Analysis, bool]: ...
    async def get_active(self, user_id: UUID) -> Analysis | None: ...
    async def get_owned(self, analysis_id: UUID, user_id: UUID) -> Analysis | None: ...
    async def save(self, analysis: Analysis) -> None: ...
    async def cancel(self, analysis: Analysis) -> None: ...


class SqlAlchemyAnalysisRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def get_active(self, user_id: UUID) -> Analysis | None:
        return cast(
            Analysis | None,
            await self._session.scalar(
                select(Analysis).where(
                    Analysis.user_id == user_id,
                    Analysis.status == "draft",
                    Analysis.intake_step != "complete",
                )
            ),
        )

    async def create_or_resume(self, user_id: UUID) -> tuple[Analysis, bool]:
        existing = await self.get_active(user_id)
        if existing is not None:
            return existing, False
        statement = (
            insert(Analysis)
            .values(user_id=user_id, intake_step="waiting_for_conversation")
            .on_conflict_do_nothing()
            .returning(Analysis.id)
        )
        created_id = (await self._session.execute(statement)).scalar_one_or_none()
        await self._session.commit()
        analysis = await self.get_active(user_id)
        if analysis is None:
            raise RuntimeError("Active analysis draft was not persisted")
        return analysis, created_id is not None

    async def get_owned(self, analysis_id: UUID, user_id: UUID) -> Analysis | None:
        return cast(
            Analysis | None,
            await self._session.scalar(
                select(Analysis).where(Analysis.id == analysis_id, Analysis.user_id == user_id)
            ),
        )

    async def save(self, analysis: Analysis) -> None:
        self._session.add(analysis)
        await self._session.commit()
        await self._session.refresh(analysis)

    async def cancel(self, analysis: Analysis) -> None:
        analysis.status = "deleted"
        analysis.normalized_conversation_json = None
        analysis.participants_json = None
        analysis.user_participant_label = None
        analysis.user_goal = None
        analysis.relationship_stage = None
        analysis.message_count = 0
        analysis.character_count = 0
        await self.save(analysis)
