"""Strict stored-result and typed report service tests."""

import copy
import json
from datetime import UTC, datetime, timedelta
from typing import cast
from uuid import UUID, uuid4

import pytest

from app.db.models import Analysis
from app.repositories.analyses import DeletionOutcome, FeedbackOutcome
from app.services.report_renderer import ReportRenderer
from app.services.report_service import (
    HISTORY_PAGE_SIZE,
    ReportRepository,
    ReportService,
    ReportStatus,
)


def payload() -> dict[str, object]:
    return {
        "quality": {"sufficient": True, "issues": [], "participants_detected": ["A", "B"]},
        "summary": "Наблюдаемый обмен сообщениями.",
        "dynamic": {"direction": "mixed", "confidence": 0.6},
        "reciprocity_score": {
            "value": 50,
            "positive_signals": [],
            "negative_signals": [],
            "limitations": [],
        },
        "observations": [{"claim": "Есть ответы.", "evidence_refs": ["m1"], "importance": "high"}],
        "hypotheses": [
            {
                "label": "Контакт",
                "explanation": "Общение может продолжиться.",
                "supporting_evidence_refs": ["m1"],
                "contradicting_evidence_refs": [],
                "confidence": "medium",
            }
        ],
        "unknowns": [],
        "next_actions": [],
        "reply_suggestions": [
            {"style": "light_low_pressure", "text": "Привет!", "why_it_fits": "Без давления."}
        ],
        "safety": {"high_risk_detected": False, "categories": []},
    }


class Analytics:
    def __init__(self) -> None:
        self.events: list[str] = []

    async def track(self, user_id: str | None, event: str, properties: object = None) -> None:
        self.events.append(event)


class MemoryReports:
    def __init__(self, rows: list[Analysis]) -> None:
        self.rows = rows

    async def get_owned(self, analysis_id: UUID, user_id: UUID) -> Analysis | None:
        return next(
            (row for row in self.rows if row.id == analysis_id and row.user_id == user_id), None
        )

    async def list_completed(
        self, user_id: UUID, page: int, page_size: int = 8
    ) -> tuple[list[Analysis], bool]:
        rows = sorted(
            (row for row in self.rows if row.user_id == user_id and row.status == "completed"),
            key=lambda row: (cast(datetime, row.completed_at), row.id),
            reverse=True,
        )
        selected = rows[page * page_size : (page + 1) * page_size + 1]
        return selected[:page_size], len(selected) > page_size

    async def record_feedback(
        self, analysis_id: UUID, user_id: UUID, score: int
    ) -> FeedbackOutcome:
        return FeedbackOutcome.RECORDED

    async def delete_owned(self, analysis_id: UUID, user_id: UUID) -> DeletionOutcome:
        return DeletionOutcome.DELETED


def analysis(
    *,
    user_id: UUID | None = None,
    status: str = "completed",
    value: dict[str, object] | None = None,
    completed_at: datetime | None = None,
) -> Analysis:
    return Analysis(
        id=uuid4(),
        user_id=user_id or uuid4(),
        status=status,
        intake_step="complete",
        result_json=value if value is not None else payload(),
        completed_at=completed_at or datetime.now(UTC),
    )


def service(rows: list[Analysis], analytics: Analytics | None = None) -> ReportService:
    return ReportService(
        cast(ReportRepository, MemoryReports(rows)), ReportRenderer(), analytics or Analytics()
    )


async def test_valid_jsonb_string_enums_reopen_without_corruption() -> None:
    row = analysis()
    outcome = await service([row]).retrieve(row.id, row.user_id)
    assert outcome.status is ReportStatus.COMPLETED
    assert outcome.result and outcome.result.dynamic.direction.value == "mixed"


@pytest.mark.parametrize("status", ["draft", "processing", "failed"])
async def test_non_completed_statuses_are_rejected(status: str) -> None:
    row = analysis(status=status)
    assert (await service([row]).retrieve(row.id, row.user_id)).status is ReportStatus.NOT_COMPLETED


async def test_not_found_wrong_owner_and_deleted_are_typed() -> None:
    row = analysis()
    reports = service([row])
    assert (await reports.retrieve(uuid4(), row.user_id)).status is ReportStatus.NOT_FOUND
    assert (await reports.retrieve(row.id, uuid4())).status is ReportStatus.NOT_FOUND
    row.status = "deleted"
    row.result_json = None
    assert (await reports.retrieve(row.id, row.user_id)).status is ReportStatus.DELETED


@pytest.mark.parametrize("mutation", ["invalid_enum", "unknown_field", "structure"])
async def test_corrupted_results_are_safely_rejected(mutation: str) -> None:
    value = copy.deepcopy(payload())
    if mutation == "invalid_enum":
        cast(dict[str, object], value["dynamic"])["direction"] = "secret_enum"
    elif mutation == "unknown_field":
        value["unknown"] = True
    else:
        value = {"summary": "broken"}
    row = analysis(value=value)
    assert (
        await service([row]).retrieve(row.id, row.user_id)
    ).status is ReportStatus.CORRUPTED_RESULT


async def test_missing_result_is_not_completed() -> None:
    row = analysis()
    row.result_json = None
    assert (await service([row]).retrieve(row.id, row.user_id)).status is ReportStatus.NOT_COMPLETED


async def test_history_is_owned_bounded_newest_first_and_has_next() -> None:
    owner, other, now = uuid4(), uuid4(), datetime.now(UTC)
    rows = [analysis(user_id=owner, completed_at=now + timedelta(seconds=i)) for i in range(10)]
    rows += [
        analysis(user_id=other, completed_at=now + timedelta(days=1)),
        analysis(user_id=owner, status="failed"),
    ]
    page = await service(rows).history(owner, 0)
    assert len(page.items) == HISTORY_PAGE_SIZE and page.has_next
    assert page.items[0].completed_at > page.items[-1].completed_at
    assert all(
        next(row for row in rows if row.id == item.analysis_id).user_id == owner
        for item in page.items
    )


async def test_feedback_and_deletion_analytics_only_on_durable_transition() -> None:
    row, analytics = analysis(), Analytics()
    reports = service([row], analytics)
    assert await reports.feedback(row.id, row.user_id, 5) is FeedbackOutcome.RECORDED
    assert await reports.delete(row.id, row.user_id) is DeletionOutcome.DELETED
    assert analytics.events == ["analysis_feedback_submitted", "analysis_deleted"]


def test_payload_fixture_itself_uses_strict_json_validation() -> None:
    from app.domain.analysis import AnalysisResult

    assert (
        AnalysisResult.model_validate_json(json.dumps(payload())).dynamic.direction.value == "mixed"
    )


async def test_analytics_failure_never_changes_feedback_or_deletion(
    caplog: pytest.LogCaptureFixture,
) -> None:
    class BrokenAnalytics:
        async def track(self, user_id: str | None, event: str, properties: object = None) -> None:
            raise RuntimeError("SECRET-PRIVATE-CONTENT")

    row = analysis()
    reports = ReportService(
        cast(ReportRepository, MemoryReports([row])), ReportRenderer(), BrokenAnalytics()
    )
    assert await reports.feedback(row.id, row.user_id, 5) is FeedbackOutcome.RECORDED
    assert await reports.delete(row.id, row.user_id) is DeletionOutcome.DELETED
    assert "SECRET-PRIVATE-CONTENT" not in caplog.text


async def test_corrupted_owned_result_keeps_only_safe_analysis_metadata() -> None:
    row = analysis(value={"summary": "SECRET-PRIVATE-CONTENT", "unknown": True})
    outcome = await service([row]).retrieve(row.id, row.user_id)
    assert outcome.status is ReportStatus.CORRUPTED_RESULT
    assert outcome.analysis is row and outcome.analysis.id == row.id
    assert outcome.result is None and outcome.report is None
    assert (await service([row]).retrieve(row.id, uuid4())).analysis is None
