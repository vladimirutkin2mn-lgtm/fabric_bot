"""Strict structured follow-up answer contract grounded in a paid report."""

import re
from typing import Annotated

from pydantic import Field, StringConstraints

from app.domain.analysis import AnalysisResult, SafetyAssessment, StrictModel

QuestionText = Annotated[
    str,
    StringConstraints(strip_whitespace=True, min_length=1, max_length=1000),
]
AnswerText = Annotated[
    str,
    StringConstraints(strip_whitespace=True, min_length=1, max_length=2500),
]
LimitationText = Annotated[
    str,
    StringConstraints(strip_whitespace=True, min_length=1, max_length=300),
]
ReportRef = Annotated[
    str,
    StringConstraints(
        pattern=(
            r"^(summary|dynamic|reciprocity_score|safety|"
            r"observations\.[0-9]+|hypotheses\.[0-9]+|unknowns\.[0-9]+|"
            r"next_actions\.[0-9]+|reply_suggestions\.[0-9]+)$"
        ),
        max_length=32,
    ),
]


class FollowUpQuestionInput(StrictModel):
    question: QuestionText


class FollowUpAnswer(StrictModel):
    answer: AnswerText
    report_refs: list[ReportRef] = Field(min_length=1, max_length=8)
    limitations: list[LimitationText] = Field(max_length=3)
    safety: SafetyAssessment


class FollowUpSemanticError(ValueError):
    """Safe validation error containing only schema locations and categories."""

    def __init__(self, issues: list[str]) -> None:
        self.issues = issues
        super().__init__(";".join(issues))


def allowed_followup_report_refs(report: AnalysisResult) -> set[str]:
    values = {"summary", "dynamic", "reciprocity_score", "safety"}
    values.update(f"observations.{index}" for index in range(len(report.observations)))
    values.update(f"hypotheses.{index}" for index in range(len(report.hypotheses)))
    values.update(f"unknowns.{index}" for index in range(len(report.unknowns)))
    values.update(f"next_actions.{index}" for index in range(len(report.next_actions)))
    values.update(f"reply_suggestions.{index}" for index in range(len(report.reply_suggestions)))
    return values


def validate_followup_semantics(answer: FollowUpAnswer, report: AnalysisResult) -> None:
    issues: list[str] = []
    allowed = allowed_followup_report_refs(report)
    if len(answer.report_refs) != len(set(answer.report_refs)):
        issues.append("report_refs:duplicate_reference")
    for reference in answer.report_refs:
        if (
            not re.fullmatch(
                r"(summary|dynamic|reciprocity_score|safety|observations\.[0-9]+|"
                r"hypotheses\.[0-9]+|unknowns\.[0-9]+|next_actions\.[0-9]+|"
                r"reply_suggestions\.[0-9]+)",
                reference,
            )
            or reference not in allowed
        ):
            issues.append("report_refs:invalid_reference")
    if report.safety.high_risk_detected and not answer.safety.high_risk_detected:
        issues.append("safety:high_risk_downgrade")
    if issues:
        raise FollowUpSemanticError(issues)
