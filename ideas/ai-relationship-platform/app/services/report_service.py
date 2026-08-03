"""Owned completed-report retrieval and validation boundary."""

from dataclasses import dataclass
from enum import StrEnum
from uuid import UUID

from pydantic import ValidationError

from app.db.models import Analysis
from app.domain.analysis import AnalysisResult
from app.repositories.analyses import SqlAlchemyAnalysisRepository
from app.services.report_renderer import RenderedReport, ReportRenderer


class ReportStatus(StrEnum):
    COMPLETED = "completed"
    NOT_FOUND = "not_found"
    NOT_COMPLETED = "not_completed"
    DELETED = "deleted"
    CORRUPTED_RESULT = "corrupted_result"


@dataclass(frozen=True)
class ReportResult:
    status: ReportStatus
    analysis: Analysis | None = None
    result: AnalysisResult | None = None
    report: RenderedReport | None = None


class ReportService:
    def __init__(self, repository: SqlAlchemyAnalysisRepository, renderer: ReportRenderer) -> None:
        self.repository, self.renderer = repository, renderer

    async def retrieve(self, analysis_id: UUID, user_id: UUID) -> ReportResult:
        analysis = await self.repository.get_owned(analysis_id, user_id)
        if analysis is None:
            return ReportResult(ReportStatus.NOT_FOUND)
        if analysis.status == "deleted":
            return ReportResult(ReportStatus.DELETED)
        if analysis.status != "completed" or analysis.result_json is None:
            return ReportResult(ReportStatus.NOT_COMPLETED)
        try:
            result = AnalysisResult.model_validate(analysis.result_json)
        except (ValidationError, ValueError, TypeError):
            return ReportResult(ReportStatus.CORRUPTED_RESULT)
        return ReportResult(ReportStatus.COMPLETED, analysis, result, self.renderer.render(result))
