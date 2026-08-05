"""Follow-up schema and report-reference validation."""

import json

import pytest

from app.domain.analysis import AnalysisResult
from app.domain.followup import (
    FollowUpAnswer,
    FollowUpSemanticError,
    validate_followup_semantics,
)
from app.providers.llm.base import LLMRequest
from app.providers.llm.stub import StubLLMClient


def report() -> AnalysisResult:
    payload = StubLLMClient()._result(LLMRequest("", "", {}, ("m1",), ("A", "B")), False)
    return AnalysisResult.model_validate_json(json.dumps(payload))


def test_valid_followup_uses_only_existing_report_references() -> None:
    answer = FollowUpAnswer.model_validate(
        {
            "answer": "Отчёт показывает смешанные сигналы, поэтому лучше уточнить ожидания прямо.",
            "report_refs": ["summary", "next_actions.0"],
            "limitations": ["Ответ основан только на уже сформированном отчёте."],
            "safety": {"high_risk_detected": False, "categories": []},
        }
    )
    validate_followup_semantics(answer, report())


def test_unknown_or_duplicate_report_reference_is_rejected() -> None:
    answer = FollowUpAnswer.model_validate(
        {
            "answer": "Ответ.",
            "report_refs": ["observations.99", "observations.99"],
            "limitations": [],
            "safety": {"high_risk_detected": False, "categories": []},
        }
    )
    with pytest.raises(FollowUpSemanticError) as error:
        validate_followup_semantics(answer, report())
    assert "report_refs:duplicate_reference" in error.value.issues
    assert "report_refs:invalid_reference" in error.value.issues


def test_followup_cannot_downgrade_primary_safety_signal() -> None:
    payload = report().model_dump(mode="json")
    payload["safety"] = {"high_risk_detected": True, "categories": ["threats"]}
    base = AnalysisResult.model_validate_json(json.dumps(payload))
    answer = FollowUpAnswer.model_validate(
        {
            "answer": "Ответ.",
            "report_refs": ["safety"],
            "limitations": [],
            "safety": {"high_risk_detected": False, "categories": []},
        }
    )
    with pytest.raises(FollowUpSemanticError) as error:
        validate_followup_semantics(answer, base)
    assert error.value.issues == ["safety:high_risk_downgrade"]
