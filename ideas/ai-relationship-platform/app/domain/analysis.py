"""Strict structured analysis contract and evidence validation."""

import re
from enum import StrEnum
from typing import Annotated

from pydantic import BaseModel, ConfigDict, Field, StringConstraints, model_validator

Text = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=2000)]
ShortText = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=500)]
EvidenceRef = Annotated[str, StringConstraints(pattern=r"^m[1-9][0-9]*$", max_length=16)]
ParticipantLabel = Annotated[str, StringConstraints(pattern=r"^[A-Z]$", max_length=1)]


class StrictModel(BaseModel):
    """Closed object model; JSON scalar types remain naturally deserializable."""

    model_config = ConfigDict(extra="forbid", strict=True)


class DynamicDirection(StrEnum):
    WARMING = "warming"
    STABLE_POSITIVE = "stable_positive"
    MIXED = "mixed"
    COOLING = "cooling"
    UNSTABLE = "unstable"
    INSUFFICIENT_DATA = "insufficient_data"


class Confidence(StrEnum):
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"


class Importance(StrEnum):
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"


class ReplyStyle(StrEnum):
    WARM_DIRECT = "warm_direct"
    LIGHT_LOW_PRESSURE = "light_low_pressure"
    BOUNDARY_SETTING = "boundary_setting"


class AnalysisMessage(StrictModel):
    id: EvidenceRef
    speaker: ParticipantLabel
    timestamp: str | None = Field(default=None, max_length=40)
    text: Text
    source_order: int = Field(ge=1)


class AnalysisRequest(StrictModel):
    messages: list[AnalysisMessage] = Field(min_length=1, max_length=500)
    participant_labels: list[ParticipantLabel] = Field(min_length=1, max_length=8)
    user_participant_label: ParticipantLabel
    user_goal: ShortText
    relationship_stage: ShortText

    @model_validator(mode="after")
    def validate_internal_consistency(self) -> "AnalysisRequest":
        message_ids = [message.id for message in self.messages]
        orders = [message.source_order for message in self.messages]
        if len(message_ids) != len(set(message_ids)):
            raise ValueError("duplicate_message_id")
        if len(orders) != len(set(orders)):
            raise ValueError("duplicate_source_order")
        if orders != sorted(orders):
            raise ValueError("inconsistent_source_order")
        if len(self.participant_labels) != len(set(self.participant_labels)):
            raise ValueError("duplicate_participant_label")
        labels = set(self.participant_labels)
        if any(message.speaker not in labels for message in self.messages):
            raise ValueError("unknown_message_speaker")
        if self.user_participant_label not in labels:
            raise ValueError("invalid_user_participant")
        return self


class AnalysisQuality(StrictModel):
    sufficient: bool
    issues: list[ShortText] = Field(max_length=10)
    participants_detected: list[ParticipantLabel] = Field(max_length=8)


class DynamicAnalysis(StrictModel):
    direction: DynamicDirection
    confidence: float = Field(ge=0.0, le=1.0)


class ReciprocityScore(StrictModel):
    value: int = Field(ge=0, le=100)
    positive_signals: list[ShortText] = Field(max_length=7)
    negative_signals: list[ShortText] = Field(max_length=7)
    limitations: list[ShortText] = Field(max_length=7)


class Observation(StrictModel):
    claim: Text
    evidence_refs: list[EvidenceRef] = Field(min_length=1, max_length=10)
    importance: Importance


class Hypothesis(StrictModel):
    label: ShortText
    explanation: Text
    supporting_evidence_refs: list[EvidenceRef] = Field(max_length=10)
    contradicting_evidence_refs: list[EvidenceRef] = Field(max_length=10)
    confidence: Confidence


class NextAction(StrictModel):
    action: Text
    why: Text
    risk: Text


class ReplySuggestion(StrictModel):
    style: ReplyStyle
    text: Text
    why_it_fits: Text


class SafetyAssessment(StrictModel):
    high_risk_detected: bool
    categories: list[ShortText] = Field(max_length=10)


class AnalysisResult(StrictModel):
    quality: AnalysisQuality
    summary: Text
    dynamic: DynamicAnalysis
    reciprocity_score: ReciprocityScore
    observations: list[Observation] = Field(max_length=7)
    hypotheses: list[Hypothesis] = Field(max_length=3)
    unknowns: list[Text] = Field(max_length=10)
    next_actions: list[NextAction] = Field(max_length=3)
    reply_suggestions: list[ReplySuggestion] = Field(max_length=3)
    safety: SafetyAssessment


class SemanticValidationError(ValueError):
    """Safe error containing categories and locations, never private values."""

    def __init__(self, issues: list[str], evidence_related: bool = True) -> None:
        self.issues = issues
        self.evidence_related = evidence_related
        super().__init__(";".join(issues))


def validate_analysis_semantics(result: AnalysisResult, request: AnalysisRequest) -> None:
    valid_ids = {message.id for message in request.messages}
    valid_labels = set(request.participant_labels)
    issues: list[str] = []
    for index, observation in enumerate(result.observations):
        if len(observation.evidence_refs) != len(set(observation.evidence_refs)):
            issues.append(f"observations.{index}.evidence_refs:duplicate_reference")
        for ref in observation.evidence_refs:
            if not re.fullmatch(r"m[1-9][0-9]*", ref) or ref not in valid_ids:
                issues.append(f"observations.{index}.evidence_refs:invalid_reference")
    for index, hypothesis in enumerate(result.hypotheses):
        for field, references in (
            ("supporting_evidence_refs", hypothesis.supporting_evidence_refs),
            ("contradicting_evidence_refs", hypothesis.contradicting_evidence_refs),
        ):
            if len(references) != len(set(references)):
                issues.append(f"hypotheses.{index}.{field}:duplicate_reference")
            for ref in references:
                if not re.fullmatch(r"m[1-9][0-9]*", ref) or ref not in valid_ids:
                    issues.append(f"hypotheses.{index}.{field}:invalid_reference")
    participant_issues: list[str] = []
    for label in result.quality.participants_detected:
        if label not in valid_labels:
            participant_issues.append("quality.participants_detected:unknown_label")
    issues.extend(participant_issues)
    if issues:
        raise SemanticValidationError(
            issues, evidence_related=bool(set(issues) - set(participant_issues))
        )
