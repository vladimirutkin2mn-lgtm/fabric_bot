"""Typed owned report, history, feedback, deletion, and analytics boundary."""

import json
import logging
from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from typing import Protocol
from uuid import UUID

from pydantic import ValidationError

from app.db.models import Analysis
from app.domain.analysis import AnalysisResult
from app.providers.analytics import AnalyticsClient
from app.repositories.analyses import DeletionOutcome, FeedbackOutcome
from app.services.data_deletion import DataDeletionOutcome, DataDeletionService
from app.services.report_renderer import RenderedReport, ReportRenderer
from app.services.sensitive_content import SensitiveContentError

logger = logging.getLogger(__name__)
HISTORY_PAGE_SIZE = 8


class ReportRepository(Protocol):
    async def get_owned(self, analysis_id: UUID, user_id: UUID) -> Analysis | None: ...
    async def list_completed(
        self, user_id: UUID, page: int, page_size: int = HISTORY_PAGE_SIZE
    ) -> tuple[list[Analysis], bool]: ...
    async def record_feedback(
        self, analysis_id: UUID, user_id: UUID, score: int
    ) -> FeedbackOutcome: ...
    async def delete_owned(self, analysis_id: UUID, user_id: UUID) -> DeletionOutcome: ...


class ReportStatus(StrEnum):
    COMPLETED = "completed"
    NOT_FOUND = "not_found"
    NOT_COMPLETED = "not_completed"
    DELETED = "deleted"
    CORRUPTED_RESULT = "corrupted_result"
    NOT_UNLOCKED = "not_unlocked"


@dataclass(frozen=True)
class ReportResult:
    status: ReportStatus
    analysis: Analysis | None = None
    result: AnalysisResult | None = None
    report: RenderedReport | None = None


@dataclass(frozen=True)
class HistoryItem:
    analysis_id: UUID
    completed_at: datetime
    relationship_stage: str | None
    access_level: str


@dataclass(frozen=True)
class HistoryPage:
    items: tuple[HistoryItem, ...]
    page: int
    has_next: bool


class ReportService:
    def __init__(
        self,
        repository: ReportRepository,
        renderer: ReportRenderer,
        analytics: AnalyticsClient,
        deletion_service: DataDeletionService | None = None,
    ) -> None:
        self._repository = repository
        self._renderer = renderer
        self._analytics = analytics
        self._deletion_service = deletion_service

    async def retrieve(self, analysis_id: UUID, user_id: UUID) -> ReportResult:
        analysis = await self._repository.get_owned(analysis_id, user_id)
        if analysis is None:
            return ReportResult(ReportStatus.NOT_FOUND)
        if analysis.status == "deleted":
            return ReportResult(ReportStatus.DELETED, analysis)
        if analysis.status != "completed":
            return ReportResult(ReportStatus.NOT_COMPLETED, analysis)
        if analysis.report_access == "none":
            return ReportResult(ReportStatus.NOT_UNLOCKED, analysis)
        try:
            private_loader = getattr(self._repository, "load_private_result", None)
            stored = await private_loader(analysis_id, user_id) if private_loader else None
            # Transitional fallback only; the backfill clears this legacy column atomically.
            result_value = stored if stored is not None else analysis.result_json
            if result_value is None:
                return ReportResult(ReportStatus.NOT_COMPLETED, analysis)
            payload = json.dumps(result_value, ensure_ascii=False)
            result = AnalysisResult.model_validate_json(payload)
        except (SensitiveContentError, ValidationError, ValueError, TypeError):
            logger.warning("report_result_corrupted analysis_id=%s", analysis_id)
            return ReportResult(ReportStatus.CORRUPTED_RESULT, analysis)
        report = (
            self._renderer.render_preview(result)
            if analysis.report_access == "preview"
            else self._renderer.render(result)
        )
        return ReportResult(ReportStatus.COMPLETED, analysis, result, report)

    async def history(self, user_id: UUID, page: int) -> HistoryPage:
        safe_page = max(page, 0)
        rows, has_next = await self._repository.list_completed(
            user_id, safe_page, HISTORY_PAGE_SIZE
        )
        items = tuple(
            HistoryItem(row.id, row.completed_at, None, row.report_access)
            for row in rows
            if row.completed_at is not None
        )
        return HistoryPage(items, safe_page, has_next)

    async def feedback(self, analysis_id: UUID, user_id: UUID, score: int) -> FeedbackOutcome:
        outcome = await self._repository.record_feedback(analysis_id, user_id, score)
        if outcome is FeedbackOutcome.RECORDED:
            await self._track(
                str(user_id),
                "analysis_feedback_submitted",
                {"analysis_id": str(analysis_id), "score": str(score)},
            )
        return outcome

    async def delete(self, analysis_id: UUID, user_id: UUID) -> DeletionOutcome:
        if self._deletion_service is not None:
            private_outcome = await self._deletion_service.delete_analysis(analysis_id, user_id)
            return {
                DataDeletionOutcome.DELETED: DeletionOutcome.DELETED,
                DataDeletionOutcome.ALREADY_DELETED: DeletionOutcome.ALREADY_DELETED,
                DataDeletionOutcome.NOT_FOUND: DeletionOutcome.NOT_FOUND,
            }[private_outcome]
        outcome = await self._repository.delete_owned(analysis_id, user_id)
        if outcome is DeletionOutcome.DELETED:
            await self._track(str(user_id), "analysis_deleted", {"analysis_id": str(analysis_id)})
        return outcome

    async def event(self, user_id: UUID, event: str, properties: dict[str, str]) -> None:
        await self._track(str(user_id), event, properties)

    def render_replies(self, result: AnalysisResult) -> RenderedReport:
        return self._renderer.render_replies(result)

    async def _track(self, user_id: str, event: str, properties: dict[str, str]) -> None:
        try:
            await self._analytics.track(user_id, event, properties)
        except Exception:
            logger.warning("report_analytics_failed event=%s", event)
