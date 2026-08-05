"""Claim-fenced one-question entitlement for an owned paid full report."""

import json
import logging
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from enum import StrEnum
from uuid import UUID, uuid4

from pydantic import ValidationError
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.db.followups import FollowUpQuestion
from app.db.models import Analysis, AnalysisPrivateContent
from app.domain.analysis import AnalysisResult
from app.domain.followup import (
    FollowUpAnswer,
    FollowUpQuestionInput,
    FollowUpSemanticError,
    allowed_followup_report_refs,
    validate_followup_semantics,
)
from app.prompts.followup_loader import load_followup_prompts
from app.prompts.loader import PromptNotFoundError, PromptSet
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
from app.services.sensitive_content import (
    ContentPurpose,
    SensitiveContentCipher,
    SensitiveContentError,
)

logger = logging.getLogger(__name__)


class FollowUpStatus(StrEnum):
    READY = "ready"
    PROCESSING = "processing"
    COMPLETED = "completed"
    NOT_ELIGIBLE = "not_eligible"
    INVALID_QUESTION = "invalid_question"
    FAILED_RELEASED = "failed_released"
    CORRUPTED_HISTORY = "corrupted_history"


@dataclass(frozen=True)
class FollowUpView:
    analysis_id: UUID
    question: str
    answer: str
    limitations: tuple[str, ...]
    safety_high_risk: bool
    completed_at: datetime


@dataclass(frozen=True)
class FollowUpResult:
    status: FollowUpStatus
    view: FollowUpView | None = None
    failure_code: str | None = None
    idempotent: bool = False


class _ReserveOutcome(StrEnum):
    CLAIMED = "claimed"
    PROCESSING = "processing"
    COMPLETED = "completed"
    NOT_ELIGIBLE = "not_eligible"


@dataclass(frozen=True)
class _Reservation:
    outcome: _ReserveOutcome
    claim_id: UUID | None = None


@dataclass(frozen=True)
class _Metadata:
    provider: str
    model: str
    attempts: int
    input_tokens: int | None
    output_tokens: int | None
    latency_ms: int | None
    provider_request_id: str | None


class FollowUpService:
    """Reserve outside provider I/O and fence every terminal write by claim ID."""

    def __init__(
        self,
        sessions: async_sessionmaker[AsyncSession],
        cipher: SensitiveContentCipher,
        llm: LLMClient,
        analytics: AnalyticsClient,
        provider: str,
        model: str,
        *,
        prompt_version: str = "followup_v1",
        lease_seconds: int = 180,
        max_question_characters: int = 1000,
        max_repair_attempts: int = 1,
        prompt_loader: Callable[[str], PromptSet] = load_followup_prompts,
    ) -> None:
        self._sessions = sessions
        self._cipher = cipher
        self._llm = llm
        self._analytics = analytics
        self._provider = provider
        self._model = model
        self._prompt_version = prompt_version
        self._lease_seconds = lease_seconds
        self._max_question_characters = max_question_characters
        self._max_repairs = max_repair_attempts
        self._prompt_loader = prompt_loader

    async def inspect(self, analysis_id: UUID, user_id: UUID) -> FollowUpResult:
        async with self._sessions() as session:
            analysis = await session.scalar(
                select(Analysis).where(Analysis.id == analysis_id, Analysis.user_id == user_id)
            )
            if not self._eligible(analysis):
                return FollowUpResult(FollowUpStatus.NOT_ELIGIBLE)
            row = await session.scalar(
                select(FollowUpQuestion).where(FollowUpQuestion.analysis_id == analysis_id)
            )
            if row is None or row.status == "available":
                return FollowUpResult(FollowUpStatus.READY)
            if row.status == "reserved":
                if row.lease_until is None or row.lease_until <= datetime.now(UTC):
                    return FollowUpResult(FollowUpStatus.READY)
                return FollowUpResult(FollowUpStatus.PROCESSING)
            try:
                return FollowUpResult(
                    FollowUpStatus.COMPLETED,
                    self._view(row),
                    idempotent=True,
                )
            except (SensitiveContentError, ValidationError, ValueError, TypeError):
                logger.warning("followup_history_corrupted analysis_id=%s", analysis_id)
                return FollowUpResult(FollowUpStatus.CORRUPTED_HISTORY)

    async def ask(self, analysis_id: UUID, user_id: UUID, question: str) -> FollowUpResult:
        try:
            parsed = FollowUpQuestionInput(question=question)
        except ValidationError:
            return FollowUpResult(FollowUpStatus.INVALID_QUESTION)
        if len(parsed.question) > self._max_question_characters:
            return FollowUpResult(FollowUpStatus.INVALID_QUESTION)

        reservation = await self._reserve(analysis_id, user_id, parsed.question)
        if reservation.outcome is _ReserveOutcome.NOT_ELIGIBLE:
            return FollowUpResult(FollowUpStatus.NOT_ELIGIBLE)
        if reservation.outcome is _ReserveOutcome.PROCESSING:
            return FollowUpResult(FollowUpStatus.PROCESSING)
        if reservation.outcome is _ReserveOutcome.COMPLETED:
            return await self.inspect(analysis_id, user_id)
        claim_id = reservation.claim_id
        if claim_id is None:
            return FollowUpResult(FollowUpStatus.PROCESSING)

        completions: list[LLMCompletion] = []
        attempts = 0
        try:
            report = await self._load_report(analysis_id, user_id)
            if report is None:
                await self._release(
                    analysis_id,
                    claim_id,
                    "analysis_not_eligible",
                    attempts,
                    completions,
                )
                return FollowUpResult(FollowUpStatus.NOT_ELIGIBLE)
            prompts = self._prompt_loader(self._prompt_version)
            report_json = json.dumps(report.model_dump(mode="json"), ensure_ascii=False)
            allowed_refs = ",".join(sorted(allowed_followup_report_refs(report)))
            request = LLMRequest(
                prompts.system,
                prompts.request.format(
                    question=parsed.question,
                    report_json=report_json,
                    allowed_report_refs=allowed_refs,
                ),
                FollowUpAnswer.model_json_schema(),
                (),
                (),
            )
            completion = await self._llm.generate_analysis(request)
            attempts += 1
            completions.append(completion)
            try:
                answer = self._validate(completion.payload, report)
            except (ValidationError, ValueError, FollowUpSemanticError) as error:
                if self._max_repairs == 0:
                    raise
                completion = await self._llm.generate_analysis(
                    LLMRequest(
                        prompts.system,
                        prompts.repair.format(
                            question=parsed.question,
                            report_json=report_json,
                            allowed_report_refs=allowed_refs,
                            validation_errors=",".join(self._safe_errors(error)),
                            prior_output=completion.payload[:20_000],
                        ),
                        request.schema,
                        (),
                        (),
                        True,
                    )
                )
                attempts += 1
                completions.append(completion)
                answer = self._validate(completion.payload, report)
            metadata = self._metadata(completions, attempts)
            completed = await self._complete(analysis_id, user_id, claim_id, answer, metadata)
            if not completed:
                return await self.inspect(analysis_id, user_id)
            result = await self.inspect(analysis_id, user_id)
            await self._track(
                user_id,
                "followup_completed",
                {
                    "analysis_id": str(analysis_id),
                    "prompt_version": self._prompt_version,
                    "attempt_count": str(attempts),
                    "repair_used": str(attempts > 1).lower(),
                },
            )
            return result
        except PromptNotFoundError:
            code = "prompt_not_found"
        except (ValidationError, ValueError, FollowUpSemanticError):
            code = "invalid_model_output"
        except LLMTimeoutError:
            code = "llm_timeout"
            attempts += 1
        except LLMRateLimitError:
            code = "llm_rate_limited"
            attempts += 1
        except LLMAuthenticationError:
            code = "llm_authentication_error"
            attempts += 1
        except LLMInvalidRequestError:
            code = "llm_invalid_request"
            attempts += 1
        except LLMTransientError:
            code = "llm_transient_error"
            attempts += 1
        except LLMUnexpectedError:
            code = "unexpected_provider_error"
            attempts += 1
        except Exception:
            code = "unexpected_pipeline_error"
        await self._release(analysis_id, claim_id, code, attempts, completions)
        await self._track(
            user_id,
            "followup_failed_released",
            {"analysis_id": str(analysis_id), "failure_code": code},
        )
        logger.warning(
            "followup_failed analysis_id=%s prompt_version=%s failure_code=%s",
            analysis_id,
            self._prompt_version,
            code,
        )
        return FollowUpResult(FollowUpStatus.FAILED_RELEASED, failure_code=code)

    async def _reserve(self, analysis_id: UUID, user_id: UUID, question: str) -> _Reservation:
        encrypted_question = self._cipher.encrypt_json(
            ContentPurpose.FOLLOW_UP_QUESTION,
            {"question": question},
        )
        now = datetime.now(UTC)
        claim_id = uuid4()
        async with self._sessions.begin() as session:
            analysis = await session.scalar(
                select(Analysis)
                .where(Analysis.id == analysis_id, Analysis.user_id == user_id)
                .with_for_update()
            )
            if not self._eligible(analysis):
                return _Reservation(_ReserveOutcome.NOT_ELIGIBLE)
            row = await session.scalar(
                select(FollowUpQuestion)
                .where(FollowUpQuestion.analysis_id == analysis_id)
                .with_for_update()
            )
            if row is not None and row.status == "completed":
                return _Reservation(_ReserveOutcome.COMPLETED)
            if (
                row is not None
                and row.status == "reserved"
                and row.lease_until is not None
                and row.lease_until > now
            ):
                return _Reservation(_ReserveOutcome.PROCESSING)
            if row is None:
                row = FollowUpQuestion(
                    analysis_id=analysis_id,
                    user_id=user_id,
                    status="reserved",
                    claim_id=claim_id,
                    lease_until=now + timedelta(seconds=self._lease_seconds),
                    question_ciphertext=encrypted_question,
                    prompt_version=self._prompt_version,
                    reservation_count=1,
                )
                session.add(row)
            else:
                row.status = "reserved"
                row.claim_id = claim_id
                row.lease_until = now + timedelta(seconds=self._lease_seconds)
                row.question_ciphertext = encrypted_question
                row.answer_ciphertext = None
                row.prompt_version = self._prompt_version
                row.reservation_count += 1
                row.last_failure_code = None
                row.completed_at = None
            await session.flush()
            return _Reservation(_ReserveOutcome.CLAIMED, claim_id)

    async def _load_report(self, analysis_id: UUID, user_id: UUID) -> AnalysisResult | None:
        async with self._sessions() as session:
            analysis = await session.scalar(
                select(Analysis).where(Analysis.id == analysis_id, Analysis.user_id == user_id)
            )
            if not self._eligible(analysis):
                return None
            private = await session.get(AnalysisPrivateContent, analysis_id)
            stored: object | None = None
            if private is not None and private.result_ciphertext is not None:
                stored = self._cipher.decrypt_json(
                    ContentPurpose.ANALYSIS_RESULT,
                    private.result_ciphertext,
                )
            elif analysis is not None:
                stored = analysis.result_json
            if stored is None:
                return None
            return AnalysisResult.model_validate_json(
                json.dumps(stored, ensure_ascii=False, separators=(",", ":"))
            )

    async def _complete(
        self,
        analysis_id: UUID,
        user_id: UUID,
        claim_id: UUID,
        answer: FollowUpAnswer,
        metadata: _Metadata,
    ) -> bool:
        encrypted_answer = self._cipher.encrypt_json(
            ContentPurpose.FOLLOW_UP_ANSWER,
            answer.model_dump(mode="json"),
        )
        async with self._sessions.begin() as session:
            analysis = await session.scalar(
                select(Analysis)
                .where(Analysis.id == analysis_id, Analysis.user_id == user_id)
                .with_for_update()
            )
            if not self._eligible(analysis):
                return False
            row = await session.scalar(
                select(FollowUpQuestion)
                .where(FollowUpQuestion.analysis_id == analysis_id)
                .with_for_update()
            )
            if row is None or row.status != "reserved" or row.claim_id != claim_id:
                return False
            row.status = "completed"
            row.answer_ciphertext = encrypted_answer
            row.claim_id = None
            row.lease_until = None
            row.completed_at = datetime.now(UTC)
            row.last_failure_code = None
            self._apply_metadata(row, metadata)
            return True

    async def _release(
        self,
        analysis_id: UUID,
        claim_id: UUID,
        code: str,
        attempts: int,
        completions: list[LLMCompletion],
    ) -> bool:
        metadata = self._metadata(completions, attempts)
        async with self._sessions.begin() as session:
            row = await session.scalar(
                select(FollowUpQuestion)
                .where(FollowUpQuestion.analysis_id == analysis_id)
                .with_for_update()
            )
            if row is None or row.status != "reserved" or row.claim_id != claim_id:
                return False
            row.status = "available"
            row.claim_id = None
            row.lease_until = None
            row.question_ciphertext = None
            row.answer_ciphertext = None
            row.completed_at = None
            row.last_failure_code = code
            self._apply_metadata(row, metadata)
            return True

    def _view(self, row: FollowUpQuestion) -> FollowUpView:
        if (
            row.question_ciphertext is None
            or row.answer_ciphertext is None
            or row.completed_at is None
        ):
            raise ValueError("followup content missing")
        question_payload = self._cipher.decrypt_json(
            ContentPurpose.FOLLOW_UP_QUESTION,
            row.question_ciphertext,
        )
        answer_payload = self._cipher.decrypt_json(
            ContentPurpose.FOLLOW_UP_ANSWER,
            row.answer_ciphertext,
        )
        if not isinstance(question_payload, dict) or not isinstance(
            question_payload.get("question"), str
        ):
            raise ValueError("followup question malformed")
        answer = FollowUpAnswer.model_validate(answer_payload)
        return FollowUpView(
            row.analysis_id,
            question_payload["question"],
            answer.answer,
            tuple(answer.limitations),
            answer.safety.high_risk_detected,
            row.completed_at,
        )

    @staticmethod
    def _eligible(analysis: Analysis | None) -> bool:
        return bool(
            analysis is not None
            and analysis.status == "completed"
            and analysis.report_access == "full"
            and analysis.cost_units > 0
            and analysis.full_access_transaction_id is not None
        )

    @staticmethod
    def _validate(payload: str, report: AnalysisResult) -> FollowUpAnswer:
        answer = FollowUpAnswer.model_validate_json(payload)
        validate_followup_semantics(answer, report)
        return answer

    @staticmethod
    def _safe_errors(error: Exception) -> list[str]:
        if isinstance(error, ValidationError):
            return [
                ".".join(str(part) for part in item["loc"]) + ":" + item["type"]
                for item in error.errors(include_input=False, include_url=False)
            ]
        if isinstance(error, FollowUpSemanticError):
            return error.issues
        return ["payload:invalid_json"]

    def _metadata(self, completions: list[LLMCompletion], attempts: int) -> _Metadata:
        last = completions[-1] if completions else None
        return _Metadata(
            last.provider if last else self._provider,
            last.model if last else self._model,
            attempts,
            sum(item.input_tokens or 0 for item in completions) or None,
            sum(item.output_tokens or 0 for item in completions) or None,
            sum(item.latency_ms or 0 for item in completions) or None,
            last.provider_request_id if last else None,
        )

    @staticmethod
    def _apply_metadata(row: FollowUpQuestion, metadata: _Metadata) -> None:
        row.llm_provider = metadata.provider
        row.model_name = metadata.model
        row.llm_attempt_count += metadata.attempts
        row.input_tokens = metadata.input_tokens
        row.output_tokens = metadata.output_tokens
        row.latency_ms = metadata.latency_ms
        row.provider_request_id = metadata.provider_request_id

    async def _track(self, user_id: UUID, event: str, properties: dict[str, str]) -> None:
        try:
            await self._analytics.track(str(user_id), event, properties)
        except Exception:
            logger.warning("followup_analytics_failed event=%s", event)
