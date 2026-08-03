"""Complete orchestration tests with deterministic in-memory boundaries."""

import asyncio
import json
import logging
from collections.abc import Callable, Mapping
from datetime import UTC, datetime
from uuid import UUID, uuid4

import pytest

from app.db.models import Analysis
from app.prompts.loader import PromptNotFoundError, PromptSet, load_prompts
from app.providers.llm.base import (
    LLMAuthenticationError,
    LLMCompletion,
    LLMInvalidRequestError,
    LLMRateLimitError,
    LLMRequest,
    LLMTimeoutError,
    LLMTransientError,
    LLMUnexpectedError,
)
from app.providers.llm.stub import StubLLMClient
from app.repositories.analyses import ClaimOutcome, LLMMetadata
from app.services.analysis_service import (
    AnalysisService,
    AnalysisServiceResult,
    AnalysisServiceStatus,
)

SECRET = "SECRET-PRIVATE-CONTENT"


def valid_payload(reference: str = "m1") -> str:
    request = LLMRequest("", "", {}, (reference,), ("A", "B"))
    return json.dumps(StubLLMClient()._result(request, False), ensure_ascii=False)


class MemoryRepository:
    def __init__(self, status: str = "draft", intake_step: str = "complete") -> None:
        self.analysis = Analysis(
            id=uuid4(),
            user_id=uuid4(),
            status=status,
            intake_step=intake_step,
            normalized_conversation_json=[
                {"id": "m1", "speaker": "A", "timestamp": None, "text": SECRET, "source_order": 1},
                {
                    "id": "m2",
                    "speaker": "B",
                    "timestamp": None,
                    "text": "fictional",
                    "source_order": 2,
                },
            ],
            participants_json={"A": "Private A", "B": "Private B"},
            user_participant_label="A",
            user_goal=SECRET,
            relationship_stage="dating",
            message_count=2,
            character_count=30,
            llm_attempt_count=0,
        )
        self.metadata: LLMMetadata | None = None
        self.complete_writes = 0
        self.failure_writes = 0

    async def get_owned(self, analysis_id: UUID, user_id: UUID) -> Analysis | None:
        return (
            self.analysis
            if (analysis_id, user_id) == (self.analysis.id, self.analysis.user_id)
            else None
        )

    async def load_processing(self, analysis_id: UUID, user_id: UUID) -> Analysis | None:
        return await self.get_owned(analysis_id, user_id)

    async def claim_processing(self, analysis_id: UUID, user_id: UUID) -> ClaimOutcome:
        if await self.get_owned(analysis_id, user_id) is None:
            return ClaimOutcome.NOT_FOUND
        outcomes = {
            "processing": ClaimOutcome.PROCESSING,
            "completed": ClaimOutcome.COMPLETED,
            "deleted": ClaimOutcome.DELETED,
            "failed": ClaimOutcome.NOT_READY,
        }
        if self.analysis.status != "draft" or self.analysis.intake_step != "complete":
            return outcomes.get(self.analysis.status, ClaimOutcome.NOT_READY)
        self.analysis.status = "processing"
        return ClaimOutcome.CLAIMED

    async def complete_processing(
        self, analysis_id: UUID, result: dict[str, object], metadata: LLMMetadata
    ) -> Analysis:
        self.complete_writes += 1
        self.analysis.status, self.analysis.result_json, self.metadata = (
            "completed",
            result,
            metadata,
        )
        self.analysis.completed_at = datetime.now(UTC)
        self._apply(metadata)
        return self.analysis

    async def fail_processing(
        self, analysis_id: UUID, failure_code: str, metadata: LLMMetadata
    ) -> Analysis:
        self.failure_writes += 1
        self.analysis.status, self.analysis.result_json = "failed", None
        self.analysis.failure_code, self.analysis.completed_at, self.metadata = (
            failure_code,
            None,
            metadata,
        )
        self._apply(metadata)
        return self.analysis

    def _apply(self, metadata: LLMMetadata) -> None:
        self.analysis.llm_provider, self.analysis.model_name = metadata.provider, metadata.model
        self.analysis.prompt_version, self.analysis.llm_attempt_count = (
            metadata.prompt_version,
            metadata.attempt_count,
        )
        self.analysis.input_tokens, self.analysis.output_tokens = (
            metadata.input_tokens,
            metadata.output_tokens,
        )
        self.analysis.latency_ms, self.analysis.provider_request_id = (
            metadata.latency_ms,
            metadata.provider_request_id,
        )


class AnalyticsRecorder:
    def __init__(self) -> None:
        self.events: list[tuple[str | None, str, Mapping[str, str] | None]] = []

    async def track(
        self, user_id: str | None, event: str, properties: Mapping[str, str] | None = None
    ) -> None:
        self.events.append((user_id, event, properties))


class FailingAnalytics(AnalyticsRecorder):
    def __init__(self, failing_event: str, cancellation: bool = False) -> None:
        super().__init__()
        self.failing_event = failing_event
        self.cancellation = cancellation

    async def track(
        self, user_id: str | None, event: str, properties: Mapping[str, str] | None = None
    ) -> None:
        if event == self.failing_event:
            if self.cancellation:
                raise asyncio.CancelledError
            raise RuntimeError(SECRET)
        await super().track(user_id, event, properties)


class ControlledLLM:
    def __init__(self, *outputs: str | Exception) -> None:
        self.outputs = list(outputs)
        self.requests: list[LLMRequest] = []

    async def generate_analysis(self, request: LLMRequest) -> LLMCompletion:
        self.requests.append(request)
        output = self.outputs.pop(0)
        if isinstance(output, Exception):
            raise output
        return LLMCompletion(output, "controlled", "actual-model", "request-1", 11, 22, 33)


def service(
    repository: MemoryRepository,
    llm: ControlledLLM,
    analytics: AnalyticsRecorder | None = None,
    prompt_loader: Callable[[str], PromptSet] = load_prompts,
) -> tuple[AnalysisService, AnalyticsRecorder]:
    recorder = analytics or AnalyticsRecorder()
    return AnalysisService(
        repository,
        llm,
        recorder,
        "configured",
        "configured-model",
        prompt_loader=prompt_loader,
    ), recorder


async def run(
    repository: MemoryRepository,
    llm: ControlledLLM,
    prompt_loader: Callable[[str], PromptSet] = load_prompts,
) -> tuple[AnalysisServiceResult, AnalyticsRecorder]:
    instance, analytics = service(repository, llm, prompt_loader=prompt_loader)
    result = await instance.analyze(repository.analysis.id, repository.analysis.user_id)
    return result, analytics


async def test_valid_first_response_persists_result_and_all_metadata() -> None:
    repository, llm = MemoryRepository(), ControlledLLM(valid_payload())
    result, analytics = await run(repository, llm)
    assert result.status == AnalysisServiceStatus.COMPLETED
    assert repository.analysis.result_json and repository.analysis.completed_at is not None
    assert (
        repository.analysis.llm_provider,
        repository.analysis.model_name,
        repository.analysis.prompt_version,
    ) == ("controlled", "actual-model", "analysis_v1")
    assert (
        repository.analysis.input_tokens,
        repository.analysis.output_tokens,
        repository.analysis.latency_ms,
    ) == (11, 22, 33)
    assert len(llm.requests) == 1
    assert [event[1] for event in analytics.events] == [
        "analysis_processing_started",
        "analysis_completed",
    ]
    assert all(event[0] == str(repository.analysis.user_id) for event in analytics.events)
    completed_properties = analytics.events[-1][2]
    assert completed_properties is not None
    assert completed_properties["latency_bucket"] == "lt_100"
    assert completed_properties["input_token_bucket"] == "lt_100"
    assert completed_properties["output_token_bucket"] == "lt_100"


@pytest.mark.parametrize(
    "invalid", ["{invalid", json.dumps({"summary": "incomplete"}), valid_payload("m999")]
)
async def test_invalid_first_output_is_repaired_exactly_once(invalid: str) -> None:
    repository, llm = MemoryRepository(), ControlledLLM(invalid, valid_payload())
    result, _ = await run(repository, llm)
    assert result.status == AnalysisServiceStatus.COMPLETED
    assert len(llm.requests) == 2 and llm.requests[1].repair
    assert repository.analysis.llm_attempt_count == 2


@pytest.mark.parametrize(
    ("first", "second", "code"),
    [
        ("{bad", "{bad", "invalid_model_output"),
        (json.dumps({"summary": "bad"}), json.dumps({"summary": "bad"}), "invalid_model_output"),
        (valid_payload("m999"), valid_payload("m999"), "invalid_evidence_refs"),
        ("{bad", valid_payload("m999"), "invalid_evidence_refs"),
        (valid_payload("m999"), "{bad", "invalid_model_output"),
    ],
)
async def test_two_invalid_outputs_fail_with_deterministic_final_category(
    first: str, second: str, code: str
) -> None:
    repository, llm = MemoryRepository(), ControlledLLM(first, second)
    result, analytics = await run(repository, llm)
    assert result.failure_code == code and repository.analysis.failure_code == code
    assert repository.analysis.status == "failed" and repository.analysis.result_json is None
    assert repository.analysis.completed_at is None and len(llm.requests) == 2
    assert [event[1] for event in analytics.events].count("analysis_failed") == 1


@pytest.mark.parametrize(
    ("error", "code"),
    [
        (LLMTimeoutError(), "llm_timeout"),
        (LLMRateLimitError(), "llm_rate_limited"),
        (LLMAuthenticationError(), "llm_authentication_error"),
        (LLMInvalidRequestError(), "llm_invalid_request"),
        (LLMTransientError(), "llm_transient_error"),
        (LLMUnexpectedError(), "unexpected_provider_error"),
    ],
)
async def test_provider_failures_are_terminal_and_never_repaired(
    error: Exception, code: str
) -> None:
    repository, llm = MemoryRepository(), ControlledLLM(error)
    result, _ = await run(repository, llm)
    assert result.failure_code == code and len(llm.requests) == 1
    assert repository.analysis.result_json is None and repository.analysis.completed_at is None


async def test_missing_prompt_is_terminal() -> None:
    repository, llm = MemoryRepository(), ControlledLLM(valid_payload())

    def missing(_: str):  # type: ignore[no-untyped-def]
        raise PromptNotFoundError("missing")

    result, _ = await run(repository, llm, prompt_loader=missing)
    assert result.failure_code == "prompt_not_found" and not llm.requests


@pytest.mark.parametrize(
    ("status", "step", "expected"),
    [
        ("draft", "waiting_for_goal", AnalysisServiceStatus.NOT_READY),
        ("deleted", "complete", AnalysisServiceStatus.DELETED),
        ("processing", "complete", AnalysisServiceStatus.ALREADY_PROCESSING),
        ("failed", "complete", AnalysisServiceStatus.NOT_READY),
    ],
)
async def test_non_claimable_analyses_never_call_provider(
    status: str, step: str, expected: AnalysisServiceStatus
) -> None:
    repository, llm = MemoryRepository(status, step), ControlledLLM(valid_payload())
    result, analytics = await run(repository, llm)
    assert result.status == expected and not llm.requests and not analytics.events


async def test_completed_result_is_idempotent_without_provider_or_duplicate_analytics() -> None:
    repository, llm = MemoryRepository(), ControlledLLM(valid_payload())
    first, analytics = await run(repository, llm)
    instance, _ = service(repository, llm, analytics)
    second = await instance.analyze(repository.analysis.id, repository.analysis.user_id)
    assert first.status == second.status == AnalysisServiceStatus.COMPLETED and second.idempotent
    assert (
        len(llm.requests) == 1
        and [event[1] for event in analytics.events].count("analysis_completed") == 1
    )


async def test_unexpected_pipeline_error_cannot_strand_processing() -> None:
    repository, llm = MemoryRepository(), ControlledLLM(RuntimeError(SECRET))
    result, _ = await run(repository, llm)
    assert result.failure_code == "unexpected_pipeline_error"
    assert repository.analysis.status == "failed" and repository.analysis.result_json is None


async def test_unexpected_prompt_loader_error_cannot_strand_processing() -> None:
    repository, llm = MemoryRepository(), ControlledLLM(valid_payload())

    def broken(_: str) -> PromptSet:
        raise RuntimeError(SECRET)

    result, _ = await run(repository, llm, prompt_loader=broken)
    assert result.failure_code == "unexpected_pipeline_error"
    assert repository.analysis.status == "failed" and not llm.requests


@pytest.mark.parametrize(
    "output", [valid_payload(), "{bad", LLMTimeoutError(), RuntimeError(SECRET)]
)
async def test_private_content_absent_from_logs_analytics_and_failure_metadata(
    output: str | Exception, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.DEBUG)
    repository = MemoryRepository()
    llm = (
        ControlledLLM(output, valid_payload())
        if isinstance(output, str) and output != valid_payload()
        else ControlledLLM(output)
    )
    _, analytics = await run(repository, llm)
    exposed = caplog.text + repr(analytics.events) + repr(repository.analysis.failure_code)
    assert SECRET not in exposed
    assert not hasattr(repository.analysis, "credits")


async def test_terminal_persistence_failure_is_propagated() -> None:
    repository, llm = MemoryRepository(), ControlledLLM(RuntimeError("pipeline"))

    async def broken(*args: object, **kwargs: object) -> Analysis:
        raise RuntimeError("database unavailable")

    repository.fail_processing = broken  # type: ignore[method-assign]
    instance, _ = service(repository, llm)
    with pytest.raises(RuntimeError, match="database unavailable"):
        await instance.analyze(repository.analysis.id, repository.analysis.user_id)


async def test_cancellation_is_never_suppressed() -> None:
    class CancelledLLM:
        async def generate_analysis(self, request: LLMRequest) -> LLMCompletion:
            raise asyncio.CancelledError

    repository = MemoryRepository()
    instance = AnalysisService(repository, CancelledLLM(), AnalyticsRecorder(), "stub", "stub")
    with pytest.raises(asyncio.CancelledError):
        await instance.analyze(repository.analysis.id, repository.analysis.user_id)


async def test_processing_analytics_failure_does_not_prevent_provider_call() -> None:
    repository, llm = MemoryRepository(), ControlledLLM(valid_payload())
    instance, _ = service(repository, llm, FailingAnalytics("analysis_processing_started"))
    result = await instance.analyze(repository.analysis.id, repository.analysis.user_id)
    assert result.status == AnalysisServiceStatus.COMPLETED
    assert len(llm.requests) == 1 and repository.complete_writes == 1
    assert repository.failure_writes == 0


async def test_completion_analytics_failure_cannot_change_or_mask_result() -> None:
    repository, llm = MemoryRepository(), ControlledLLM(valid_payload())
    instance, _ = service(repository, llm, FailingAnalytics("analysis_completed"))
    result = await instance.analyze(repository.analysis.id, repository.analysis.user_id)
    assert result.status == AnalysisServiceStatus.COMPLETED
    assert repository.analysis.status == "completed" and repository.analysis.result_json
    assert repository.complete_writes == 1 and repository.failure_writes == 0


async def test_failure_analytics_failure_cannot_change_or_mask_typed_failure() -> None:
    repository, llm = MemoryRepository(), ControlledLLM(LLMTimeoutError())
    instance, _ = service(repository, llm, FailingAnalytics("analysis_failed"))
    result = await instance.analyze(repository.analysis.id, repository.analysis.user_id)
    assert result.status == AnalysisServiceStatus.FAILED
    assert repository.analysis.status == "failed" and repository.analysis.result_json is None
    assert repository.failure_writes == 1 and repository.complete_writes == 0


@pytest.mark.parametrize(
    "event", ["analysis_processing_started", "analysis_completed", "analysis_failed"]
)
async def test_analytics_exception_private_text_is_never_logged_or_stored(
    event: str, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.WARNING)
    repository = MemoryRepository()
    llm = (
        ControlledLLM(LLMTimeoutError())
        if event == "analysis_failed"
        else ControlledLLM(valid_payload())
    )
    instance, _ = service(repository, llm, FailingAnalytics(event))
    await instance.analyze(repository.analysis.id, repository.analysis.user_id)
    assert SECRET not in caplog.text
    assert SECRET not in repr(repository.analysis.failure_code)
    assert repository.complete_writes + repository.failure_writes == 1


async def test_analytics_cancellation_is_propagated() -> None:
    repository, llm = MemoryRepository(), ControlledLLM(valid_payload())
    instance, _ = service(
        repository,
        llm,
        FailingAnalytics("analysis_processing_started", cancellation=True),
    )
    with pytest.raises(asyncio.CancelledError):
        await instance.analyze(repository.analysis.id, repository.analysis.user_id)
    assert not llm.requests
