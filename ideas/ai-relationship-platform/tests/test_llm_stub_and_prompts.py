import pytest
from pydantic import ValidationError

from app.domain.analysis import AnalysisResult
from app.prompts.loader import PromptNotFoundError, load_prompts
from app.providers.llm.base import (
    LLMAuthenticationError,
    LLMRateLimitError,
    LLMRequest,
    LLMTimeoutError,
    LLMTransientError,
)
from app.providers.llm.stub import StubLLMClient


def llm_request(repair: bool = False) -> LLMRequest:
    return LLMRequest(
        "system", "request", AnalysisResult.model_json_schema(), ("m1",), ("A", "B"), repair
    )


def test_prompt_loader_is_versioned_and_rejects_traversal() -> None:
    prompts = load_prompts("analysis_v1")
    assert prompts.version == "analysis_v1" and prompts.system and prompts.repair
    for version in ("missing", "../analysis_v1"):
        with pytest.raises(PromptNotFoundError):
            load_prompts(version)


@pytest.mark.parametrize(
    "behavior", ["success", "invalid_json", "invalid_schema", "invalid_evidence_ref"]
)
async def test_stub_payload_behaviors(behavior: str) -> None:
    client = StubLLMClient(behavior=behavior)  # type: ignore[arg-type]
    completion = await client.generate_analysis(llm_request())
    assert completion.provider == "stub" and completion.payload


@pytest.mark.parametrize(
    ("behavior", "error"),
    [
        ("timeout", LLMTimeoutError),
        ("rate_limit", LLMRateLimitError),
        ("authentication_error", LLMAuthenticationError),
        ("transport_error", LLMTransientError),
    ],
)
async def test_stub_error_behaviors(behavior: str, error: type[Exception]) -> None:
    with pytest.raises(error):
        await StubLLMClient(behavior=behavior).generate_analysis(llm_request())  # type: ignore[arg-type]


async def test_stub_repair_success_and_failure() -> None:
    success = StubLLMClient(behavior="repair_success")
    AnalysisResult.model_validate_json((await success.generate_analysis(llm_request(True))).payload)
    failure = StubLLMClient(behavior="repair_failure")
    with pytest.raises(ValidationError):
        AnalysisResult.model_validate_json(
            (await failure.generate_analysis(llm_request(True))).payload
        )
