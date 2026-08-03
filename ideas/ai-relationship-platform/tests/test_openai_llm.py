"""OpenAI adapter contract tests; no network or real key."""

from dataclasses import dataclass, field
from types import SimpleNamespace
from typing import Any, cast

import httpx
import openai
import pytest
from openai import AsyncOpenAI

from app.domain.analysis import AnalysisResult
from app.providers.llm.base import (
    LLMAuthenticationError,
    LLMInvalidRequestError,
    LLMRateLimitError,
    LLMRequest,
    LLMTimeoutError,
    LLMTransientError,
    LLMUnexpectedError,
)
from app.providers.llm.openai import OpenAILLMClient, openai_strict_schema

SECRET = "SECRET-PRIVATE-CONTENT"


@dataclass
class FakeResponse:
    output_text: str = "{}"
    model: str = "actual-model"
    usage: object | None = field(
        default_factory=lambda: SimpleNamespace(input_tokens=17, output_tokens=29)
    )
    _request_id: str = "req-123"


class FakeResponses:
    def __init__(self, *results: object) -> None:
        self.results = list(results or (FakeResponse(),))
        self.calls: list[dict[str, object]] = []

    async def create(self, **kwargs: object) -> object:
        self.calls.append(kwargs)
        result = self.results.pop(0)
        if isinstance(result, BaseException):
            raise result
        return result


class FakeClient:
    def __init__(self, *results: object) -> None:
        self.responses = FakeResponses(*results)


def request() -> LLMRequest:
    return LLMRequest(
        "system " + SECRET,
        "user " + SECRET,
        AnalysisResult.model_json_schema(),
        ("m1",),
        ("A", "B"),
    )


def adapter(client: FakeClient, attempts: int = 2) -> OpenAILLMClient:
    return OpenAILLMClient(
        "not-a-real-key",
        "configured-model",
        12.5,
        attempts,
        cast(AsyncOpenAI, client),
    )


async def test_request_contract_and_metadata_extraction() -> None:
    client = FakeClient()
    completion = await adapter(client).generate_analysis(request())
    call = client.responses.calls[0]
    assert call["model"] == "configured-model"
    assert call["input"] == [
        {"type": "message", "role": "system", "content": "system " + SECRET},
        {"type": "message", "role": "user", "content": "user " + SECRET},
    ]
    format_ = cast_dict(cast_dict(call["text"])["format"])
    assert format_["type"] == "json_schema" and format_["strict"] is True
    assert call["store"] is False and call["timeout"] == 12.5
    assert (completion.model, completion.provider_request_id) == ("actual-model", "req-123")
    assert (completion.input_tokens, completion.output_tokens) == (17, 29)


def cast_dict(value: object) -> dict[str, Any]:
    assert isinstance(value, dict)
    return value


async def test_missing_usage_is_safe() -> None:
    completion = await adapter(FakeClient(FakeResponse(usage=None))).generate_analysis(request())
    assert completion.input_tokens is None and completion.output_tokens is None


def test_provider_schema_removes_unsupported_keywords_recursively() -> None:
    schema = cast_dict(openai_strict_schema(AnalysisResult.model_json_schema()))
    forbidden = {
        "default",
        "examples",
        "format",
        "maxItems",
        "maxLength",
        "maximum",
        "minItems",
        "minLength",
        "minimum",
        "pattern",
        "title",
    }

    def visit(value: object) -> None:
        if isinstance(value, dict):
            assert forbidden.isdisjoint(value)
            if value.get("type") == "object" and isinstance(value.get("properties"), dict):
                assert value["required"] == list(value["properties"])
                assert value["additionalProperties"] is False
            for child in value.values():
                visit(child)
        elif isinstance(value, list):
            for child in value:
                visit(child)

    visit(schema)
    assert schema["additionalProperties"] is False and "$defs" in schema


def sdk_request() -> httpx.Request:
    return httpx.Request("POST", "https://api.openai.com/v1/responses")


def response(status: int) -> httpx.Response:
    return httpx.Response(status, request=sdk_request(), json={"error": {"message": "safe"}})


@pytest.mark.parametrize(
    ("sdk_error", "mapped"),
    [
        (openai.APITimeoutError(request=sdk_request()), LLMTimeoutError),
        (openai.RateLimitError("rate", response=response(429), body=None), LLMRateLimitError),
        (
            openai.AuthenticationError("auth", response=response(401), body=None),
            LLMAuthenticationError,
        ),
        (
            openai.PermissionDeniedError("permission", response=response(403), body=None),
            LLMInvalidRequestError,
        ),
        (openai.BadRequestError("bad", response=response(400), body=None), LLMInvalidRequestError),
        (
            openai.NotFoundError("missing", response=response(404), body=None),
            LLMInvalidRequestError,
        ),
    ],
)
async def test_non_retryable_sdk_errors_are_mapped_once(
    sdk_error: Exception, mapped: type[Exception]
) -> None:
    client = FakeClient(sdk_error)
    with pytest.raises(mapped):
        await adapter(client, 3).generate_analysis(request())
    assert len(client.responses.calls) == 1


@pytest.mark.parametrize(
    "sdk_error",
    [
        openai.APIConnectionError(request=sdk_request()),
        openai.InternalServerError("server", response=response(500), body=None),
    ],
)
async def test_transient_errors_retry_to_configured_limit(sdk_error: Exception) -> None:
    client = FakeClient(sdk_error, sdk_error, sdk_error)
    with pytest.raises(LLMTransientError):
        await adapter(client, 3).generate_analysis(request())
    assert len(client.responses.calls) == 3


async def test_transient_retry_can_succeed() -> None:
    client = FakeClient(openai.APIConnectionError(request=sdk_request()), FakeResponse())
    result = await adapter(client).generate_analysis(request())
    assert result.model == "actual-model" and len(client.responses.calls) == 2


async def test_unexpected_sdk_error_is_mapped_without_private_logging(
    caplog: pytest.LogCaptureFixture,
) -> None:
    client = FakeClient(openai.OpenAIError(SECRET))
    with pytest.raises(LLMUnexpectedError):
        await adapter(client).generate_analysis(request())
    assert SECRET not in caplog.text
