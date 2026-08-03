"""Claim, validate, optionally repair, and persist an LLM analysis."""

import json
import logging
from collections.abc import Callable
from dataclasses import dataclass
from enum import StrEnum
from uuid import UUID

from pydantic import ValidationError

from app.config import Settings
from app.domain.analysis import (
    AnalysisRequest,
    AnalysisResult,
    SemanticValidationError,
    validate_analysis_semantics,
)
from app.prompts.loader import PromptNotFoundError, PromptSet, load_prompts
from app.providers.analytics import AnalyticsClient
from app.providers.llm.base import (
    LLMAuthenticationError,
    LLMClient,
    LLMCompletion,
    LLMInvalidRequestError,
    LLMRateLimitError,
    LLMRequest,
    LLMTimeoutError,
    LLMTransientError,
    LLMUnexpectedError,
)
from app.repositories.analyses import AnalysisProcessingRepository, ClaimOutcome, LLMMetadata

logger = logging.getLogger(__name__)


class AnalysisServiceStatus(StrEnum):
    COMPLETED = "completed"
    FAILED = "failed"
    ALREADY_PROCESSING = "already_processing"
    NOT_READY = "not_ready"
    DELETED = "deleted"
    NOT_FOUND = "not_found"


@dataclass(frozen=True)
class AnalysisServiceResult:
    status: AnalysisServiceStatus
    result: AnalysisResult | None = None
    failure_code: str | None = None
    idempotent: bool = False


class AnalysisService:
    def __init__(
        self,
        analyses: AnalysisProcessingRepository,
        llm: LLMClient,
        analytics: AnalyticsClient,
        provider: str,
        model: str,
        prompt_version: str = "analysis_v1",
        max_repair_attempts: int = 1,
        prompt_loader: Callable[[str], PromptSet] = load_prompts,
    ) -> None:
        self._analyses, self._llm, self._analytics = analyses, llm, analytics
        self._provider, self._model, self._prompt_version = provider, model, prompt_version
        self._max_repairs = max_repair_attempts
        self._prompt_loader = prompt_loader

    async def analyze(self, analysis_id: UUID, user_id: UUID) -> AnalysisServiceResult:
        outcome = await self._analyses.claim_processing(analysis_id, user_id)
        if outcome == ClaimOutcome.COMPLETED:
            stored = await self._analyses.get_owned(analysis_id, user_id)
            result = (
                AnalysisResult.model_validate_json(json.dumps(stored.result_json))
                if stored and stored.result_json
                else None
            )
            return AnalysisServiceResult(AnalysisServiceStatus.COMPLETED, result, idempotent=True)
        mapped = {
            ClaimOutcome.PROCESSING: (
                AnalysisServiceStatus.ALREADY_PROCESSING,
                "already_processing",
            ),
            ClaimOutcome.DELETED: (AnalysisServiceStatus.DELETED, "analysis_deleted"),
            ClaimOutcome.NOT_READY: (AnalysisServiceStatus.NOT_READY, "analysis_not_ready"),
            ClaimOutcome.NOT_FOUND: (AnalysisServiceStatus.NOT_FOUND, "analysis_not_ready"),
        }
        if outcome != ClaimOutcome.CLAIMED:
            status, code = mapped[outcome]
            return AnalysisServiceResult(status, failure_code=code)
        attempts = 0
        completions: list[LLMCompletion] = []
        try:
            await self._track_best_effort(
                str(user_id),
                "analysis_processing_started",
                {
                    "analysis_id": str(analysis_id),
                    "provider": self._provider,
                    "model": self._model,
                    "prompt_version": self._prompt_version,
                },
            )
            analysis = await self._analyses.load_processing(analysis_id, user_id)
            if analysis is None:
                raise RuntimeError("claimed analysis unavailable")
            prompts = self._prompt_loader(self._prompt_version)
            request = AnalysisRequest.model_validate(
                {
                    "messages": analysis.normalized_conversation_json,
                    "participant_labels": list((analysis.participants_json or {}).keys()),
                    "user_participant_label": analysis.user_participant_label,
                    "user_goal": analysis.user_goal,
                    "relationship_stage": analysis.relationship_stage,
                }
            )
            user_prompt = prompts.request.format(
                participant_labels=",".join(request.participant_labels),
                user_participant_label=request.user_participant_label,
                user_goal=request.user_goal,
                relationship_stage=request.relationship_stage,
                messages_json=json.dumps(
                    [message.model_dump(mode="json") for message in request.messages],
                    ensure_ascii=False,
                ),
            )
            llm_request = LLMRequest(
                prompts.system,
                user_prompt,
                AnalysisResult.model_json_schema(),
                tuple(message.id for message in request.messages),
                tuple(request.participant_labels),
            )
            completion = await self._llm.generate_analysis(llm_request)
            attempts += 1
            completions.append(completion)
            try:
                result = self._validate(completion.payload, request)
            except (ValidationError, ValueError, SemanticValidationError) as error:
                if self._max_repairs == 0:
                    raise
                safe_errors = self._safe_errors(error)
                repair_prompt = prompts.repair.format(
                    validation_errors=",".join(safe_errors),
                    participant_labels=",".join(request.participant_labels),
                    message_ids=",".join(llm_request.message_ids),
                    prior_output=completion.payload[:20_000],
                )
                completion = await self._llm.generate_analysis(
                    LLMRequest(
                        prompts.system,
                        repair_prompt,
                        llm_request.schema,
                        llm_request.message_ids,
                        llm_request.participant_labels,
                        True,
                    )
                )
                attempts += 1
                completions.append(completion)
                result = self._validate(completion.payload, request)
            metadata = self._metadata(completions, attempts)
            await self._analyses.complete_processing(
                analysis_id, result.model_dump(mode="json"), metadata
            )
            await self._track_best_effort(
                str(user_id),
                "analysis_completed",
                self._properties(analysis_id, metadata, attempts > 1),
            )
            logger.info(
                "analysis_completed analysis_id=%s provider=%s model=%s "
                "prompt_version=%s attempt=%s",
                analysis_id,
                metadata.provider,
                metadata.model,
                metadata.prompt_version,
                attempts,
            )
            return AnalysisServiceResult(AnalysisServiceStatus.COMPLETED, result)
        except PromptNotFoundError:
            return await self._fail(user_id, analysis_id, "prompt_not_found", attempts, completions)
        except (ValidationError, ValueError, SemanticValidationError) as error:
            return await self._fail(
                user_id, analysis_id, self._validation_failure_code(error), attempts, completions
            )
        except LLMTimeoutError:
            return await self._fail(user_id, analysis_id, "llm_timeout", attempts + 1, completions)
        except LLMRateLimitError:
            return await self._fail(
                user_id, analysis_id, "llm_rate_limited", attempts + 1, completions
            )
        except LLMAuthenticationError:
            return await self._fail(
                user_id, analysis_id, "llm_authentication_error", attempts + 1, completions
            )
        except LLMInvalidRequestError:
            return await self._fail(
                user_id, analysis_id, "llm_invalid_request", attempts + 1, completions
            )
        except LLMTransientError:
            return await self._fail(
                user_id, analysis_id, "llm_transient_error", attempts + 1, completions
            )
        except LLMUnexpectedError:
            return await self._fail(
                user_id, analysis_id, "unexpected_provider_error", attempts + 1, completions
            )
        except Exception:
            return await self._fail(
                user_id, analysis_id, "unexpected_pipeline_error", attempts, completions
            )

    @staticmethod
    def _validate(payload: str, request: AnalysisRequest) -> AnalysisResult:
        result = AnalysisResult.model_validate_json(payload)
        validate_analysis_semantics(result, request)
        return result

    @staticmethod
    def _safe_errors(error: Exception) -> list[str]:
        if isinstance(error, ValidationError):
            return [
                ".".join(str(part) for part in item["loc"]) + ":" + item["type"]
                for item in error.errors(include_input=False, include_url=False)
            ]
        if isinstance(error, SemanticValidationError):
            return error.issues
        return ["payload:invalid_json"]

    @staticmethod
    def _validation_failure_code(error: Exception) -> str:
        if isinstance(error, SemanticValidationError) and error.evidence_related:
            return "invalid_evidence_refs"
        return "invalid_model_output"

    def _metadata(self, completions: list[LLMCompletion], attempts: int) -> LLMMetadata:
        last = completions[-1] if completions else None
        return LLMMetadata(
            last.provider if last else self._provider,
            last.model if last else self._model,
            self._prompt_version,
            attempts,
            sum(c.input_tokens or 0 for c in completions) or None,
            sum(c.output_tokens or 0 for c in completions) or None,
            sum(c.latency_ms or 0 for c in completions) or None,
            last.provider_request_id if last else None,
        )

    def _properties(
        self,
        analysis_id: UUID,
        metadata: LLMMetadata,
        repair: bool,
        failure_code: str | None = None,
    ) -> dict[str, str]:
        values = {
            "analysis_id": str(analysis_id),
            "provider": metadata.provider,
            "model": metadata.model,
            "prompt_version": metadata.prompt_version,
            "attempt_count": str(metadata.attempt_count),
            "repair_used": str(repair).lower(),
            "latency_bucket": self._bucket(metadata.latency_ms),
            "input_token_bucket": self._bucket(metadata.input_tokens),
            "output_token_bucket": self._bucket(metadata.output_tokens),
        }
        if failure_code:
            values["failure_code"] = failure_code
        return values

    @staticmethod
    def _bucket(value: int | None) -> str:
        if value is None:
            return "unknown"
        for boundary in (10, 100, 1_000, 10_000):
            if value < boundary:
                return f"lt_{boundary}"
        return "gte_10000"

    async def _fail(
        self,
        user_id: UUID,
        analysis_id: UUID,
        code: str,
        attempts: int,
        completions: list[LLMCompletion],
    ) -> AnalysisServiceResult:
        metadata = self._metadata(completions, attempts)
        await self._analyses.fail_processing(analysis_id, code, metadata)
        await self._track_best_effort(
            str(user_id),
            "analysis_failed",
            self._properties(analysis_id, metadata, attempts > 1, code),
        )
        logger.warning(
            "analysis_failed analysis_id=%s provider=%s model=%s prompt_version=%s "
            "attempt=%s failure_code=%s",
            analysis_id,
            metadata.provider,
            metadata.model,
            metadata.prompt_version,
            attempts,
            code,
        )
        return AnalysisServiceResult(AnalysisServiceStatus.FAILED, failure_code=code)

    async def _track_best_effort(
        self, user_id: str, event: str, properties: dict[str, str]
    ) -> None:
        """Keep product analytics outside the correctness path.

        ``CancelledError`` is intentionally not caught because it derives from
        ``BaseException`` rather than ``Exception`` on supported Python versions.
        """
        try:
            await self._analytics.track(user_id, event, properties)
        except Exception:
            logger.warning(
                "analytics_failed analysis_id=%s event=%s analytics_error=provider_error",
                properties["analysis_id"],
                event,
            )


def create_analysis_service(
    settings: Settings,
    analyses: AnalysisProcessingRepository,
    llm: LLMClient,
    analytics: AnalyticsClient,
) -> AnalysisService:
    """Compose the service without duplicating runtime policy defaults."""
    return AnalysisService(
        analyses,
        llm,
        analytics,
        settings.llm_provider,
        settings.llm_model,
        settings.llm_prompt_version,
        settings.llm_max_repair_attempts,
    )
