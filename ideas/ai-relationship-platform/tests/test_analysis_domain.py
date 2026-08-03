"""Strict result and semantic evidence tests."""

import json
from typing import cast

import pytest
from pydantic import ValidationError

from app.domain.analysis import (
    AnalysisRequest,
    AnalysisResult,
    SemanticValidationError,
    validate_analysis_semantics,
)
from app.providers.llm.base import LLMRequest
from app.providers.llm.stub import StubLLMClient


def request() -> AnalysisRequest:
    return AnalysisRequest.model_validate(
        {
            "messages": [
                {"id": "m1", "speaker": "A", "timestamp": None, "text": "Тест", "source_order": 1}
            ],
            "participant_labels": ["A", "B"],
            "user_participant_label": "A",
            "user_goal": "Понять общение",
            "relationship_stage": "dating",
        }
    )


async def valid_payload() -> dict[str, object]:
    completion = await StubLLMClient().generate_analysis(
        LLMRequest("s", "u", AnalysisResult.model_json_schema(), ("m1",), ("A", "B"))
    )
    return cast(dict[str, object], json.loads(completion.payload))


async def test_valid_result_and_generated_schema() -> None:
    result = AnalysisResult.model_validate(await valid_payload())
    validate_analysis_semantics(result, request())
    assert AnalysisResult.model_json_schema()["additionalProperties"] is False


@pytest.mark.parametrize(
    "mutation",
    ["unknown", "blank", "confidence", "score", "observations", "hypotheses", "actions", "replies"],
)
async def test_strict_constraints(mutation: str) -> None:
    payload = await valid_payload()
    if mutation == "unknown":
        payload["unexpected"] = True
    elif mutation == "blank":
        payload["summary"] = "   "
    elif mutation == "confidence":
        payload["dynamic"]["confidence"] = 1.1  # type: ignore[index]
    elif mutation == "score":
        payload["reciprocity_score"]["value"] = -1  # type: ignore[index]
    elif mutation == "observations":
        payload["observations"] = payload["observations"] * 8  # type: ignore[operator]
    elif mutation == "hypotheses":
        payload["hypotheses"] = payload["hypotheses"] * 4  # type: ignore[operator]
    elif mutation == "actions":
        payload["next_actions"] = payload["next_actions"] * 4  # type: ignore[operator]
    else:
        payload["reply_suggestions"] = payload["reply_suggestions"] * 4  # type: ignore[operator]
    with pytest.raises(ValidationError):
        AnalysisResult.model_validate(payload)


@pytest.mark.parametrize("path", ["observation", "supporting", "contradicting", "participant"])
async def test_semantic_unknown_references_are_not_silently_changed(path: str) -> None:
    payload = await valid_payload()
    if path == "observation":
        payload["observations"][0]["evidence_refs"] = ["m999"]  # type: ignore[index]
    elif path == "supporting":
        payload["hypotheses"][0]["supporting_evidence_refs"] = ["m999"]  # type: ignore[index]
    elif path == "contradicting":
        payload["hypotheses"][0]["contradicting_evidence_refs"] = ["m999"]  # type: ignore[index]
    else:
        payload["quality"]["participants_detected"] = ["C"]  # type: ignore[index]
    result = AnalysisResult.model_validate(payload)
    with pytest.raises(SemanticValidationError):
        validate_analysis_semantics(result, request())
    assert "m999" in result.model_dump_json() or path == "participant"
