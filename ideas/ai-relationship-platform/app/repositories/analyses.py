"""Analysis persistence boundary."""

from datetime import UTC, datetime
from enum import StrEnum
from typing import Protocol, cast
from uuid import UUID

from sqlalchemy import select, update
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.engine import CursorResult
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import Analysis


class AnalysisRepository(Protocol):
    async def create_or_resume(self, user_id: UUID) -> tuple[Analysis, bool]: ...
    async def get_active(self, user_id: UUID) -> Analysis | None: ...
    async def get_owned(self, analysis_id: UUID, user_id: UUID) -> Analysis | None: ...
    async def save(self, analysis: Analysis) -> None: ...
    async def cancel(self, analysis: Analysis) -> None: ...


class ClaimOutcome(StrEnum):
    CLAIMED = "claimed"
    COMPLETED = "completed"
    PROCESSING = "processing"
    DELETED = "deleted"
    NOT_READY = "not_ready"
    NOT_FOUND = "not_found"


class LLMMetadata:
    def __init__(
        self,
        provider: str,
        model: str,
        prompt_version: str,
        attempt_count: int,
        input_tokens: int | None = None,
        output_tokens: int | None = None,
        latency_ms: int | None = None,
        provider_request_id: str | None = None,
    ) -> None:
        self.provider, self.model, self.prompt_version = provider, model, prompt_version
        self.attempt_count, self.input_tokens, self.output_tokens = (
            attempt_count,
            input_tokens,
            output_tokens,
        )
        self.latency_ms, self.provider_request_id = latency_ms, provider_request_id


class AnalysisProcessingRepository(Protocol):
    async def get_owned(self, analysis_id: UUID, user_id: UUID) -> Analysis | None: ...
    async def load_processing(self, analysis_id: UUID, user_id: UUID) -> Analysis | None: ...
    async def claim_processing(self, analysis_id: UUID, user_id: UUID) -> ClaimOutcome: ...
    async def complete_processing(
        self, analysis_id: UUID, result: dict[str, object], metadata: LLMMetadata
    ) -> Analysis: ...
    async def fail_processing(
        self, analysis_id: UUID, failure_code: str, metadata: LLMMetadata
    ) -> Analysis: ...


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

    async def load_processing(self, analysis_id: UUID, user_id: UUID) -> Analysis | None:
        """Load claimed input and close the read transaction before network I/O."""
        analysis = await self.get_owned(analysis_id, user_id)
        await self._session.commit()
        return analysis

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

    async def claim_processing(self, analysis_id: UUID, user_id: UUID) -> ClaimOutcome:
        statement = (
            update(Analysis)
            .where(
                Analysis.id == analysis_id,
                Analysis.user_id == user_id,
                Analysis.status == "draft",
                Analysis.intake_step == "complete",
            )
            .values(status="processing", processing_started_at=datetime.now(UTC), failure_code=None)
            .returning(Analysis.id)
        )
        claimed = (await self._session.execute(statement)).scalar_one_or_none()
        await self._session.commit()
        if claimed is not None:
            return ClaimOutcome.CLAIMED
        current = await self.get_owned(analysis_id, user_id)
        if current is None:
            return ClaimOutcome.NOT_FOUND
        return {
            "completed": ClaimOutcome.COMPLETED,
            "processing": ClaimOutcome.PROCESSING,
            "deleted": ClaimOutcome.DELETED,
        }.get(current.status, ClaimOutcome.NOT_READY)

    async def complete_processing(
        self, analysis_id: UUID, result: dict[str, object], metadata: LLMMetadata
    ) -> Analysis:
        await self._terminal_update(analysis_id, "completed", result, None, metadata)
        analysis = await self._session.get(Analysis, analysis_id)
        if analysis is None:
            raise RuntimeError("Analysis disappeared")
        return analysis

    async def fail_processing(
        self, analysis_id: UUID, failure_code: str, metadata: LLMMetadata
    ) -> Analysis:
        await self._terminal_update(analysis_id, "failed", None, failure_code, metadata)
        analysis = await self._session.get(Analysis, analysis_id)
        if analysis is None:
            raise RuntimeError("Analysis disappeared")
        return analysis

    async def _terminal_update(
        self,
        analysis_id: UUID,
        status: str,
        result: dict[str, object] | None,
        failure_code: str | None,
        metadata: LLMMetadata,
    ) -> None:
        values = {
            "status": status,
            "result_json": result,
            "failure_code": failure_code,
            "llm_provider": metadata.provider,
            "model_name": metadata.model,
            "prompt_version": metadata.prompt_version,
            "llm_attempt_count": metadata.attempt_count,
            "input_tokens": metadata.input_tokens,
            "output_tokens": metadata.output_tokens,
            "latency_ms": metadata.latency_ms,
            "provider_request_id": metadata.provider_request_id,
            "completed_at": datetime.now(UTC),
        }
        statement = (
            update(Analysis)
            .where(Analysis.id == analysis_id, Analysis.status == "processing")
            .values(**values)
        )
        result_proxy = cast(CursorResult[object], await self._session.execute(statement))
        if result_proxy.rowcount != 1:
            await self._session.rollback()
            raise RuntimeError("Analysis is not processing")
        await self._session.commit()
