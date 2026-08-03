"""Financial orchestration around the existing analysis pipeline."""

import json
from dataclasses import dataclass
from enum import StrEnum
from uuid import UUID

from pydantic import ValidationError
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.db.models import Analysis
from app.domain.analysis import AnalysisResult
from app.services.analysis_service import AnalysisRunner, AnalysisServiceStatus
from app.services.credits_service import CreditsService, RefundOutcome, SpendOutcome
from app.services.preview_entitlement import PreviewEntitlementService, PreviewOutcome


class MonetizedStatus(StrEnum):
    PREVIEW_COMPLETED = "preview_completed"
    FULL_COMPLETED = "full_completed"
    ALREADY_PROCESSING = "already_processing"
    INSUFFICIENT_CREDITS = "insufficient_credits"
    PREVIEW_UNAVAILABLE = "preview_unavailable"
    NOT_READY = "not_ready"
    TECHNICAL_FAILURE_REFUNDED = "technical_failure_refunded"
    TECHNICAL_FAILURE_ALREADY_REFUNDED = "technical_failure_already_refunded"
    CORRUPTED_RESULT = "corrupted_result"
    NOT_FOUND = "not_found"
    DELETED = "deleted"


@dataclass(frozen=True)
class MonetizedAnalysisResult:
    status: MonetizedStatus
    result: AnalysisResult | None = None
    balance: int | None = None


class MonetizedAnalysisService:
    def __init__(
        self,
        sessions: async_sessionmaker[AsyncSession],
        credits: CreditsService,
        previews: PreviewEntitlementService,
        analysis: AnalysisRunner,
        analysis_price: int,
    ) -> None:
        self._sessions, self._credits, self._previews = sessions, credits, previews
        self._analysis, self._price = analysis, analysis_price

    async def run_preview(self, analysis_id: UUID, user_id: UUID) -> MonetizedAnalysisResult:
        reservation = await self._previews.reserve_preview(user_id, analysis_id)
        if reservation not in {
            PreviewOutcome.RESERVED,
            PreviewOutcome.ALREADY_RESERVED_SAME_ANALYSIS,
        }:
            return MonetizedAnalysisResult(
                MonetizedStatus.NOT_FOUND
                if reservation in {PreviewOutcome.USER_NOT_FOUND, PreviewOutcome.ANALYSIS_NOT_FOUND}
                else MonetizedStatus.PREVIEW_UNAVAILABLE
            )
        outcome = await self._analysis.analyze(analysis_id, user_id)
        if outcome.status is AnalysisServiceStatus.COMPLETED and outcome.result is not None:
            await self._set_access(analysis_id, user_id, "preview", 0, None)
            await self._previews.consume_preview(user_id, analysis_id)
            return MonetizedAnalysisResult(MonetizedStatus.PREVIEW_COMPLETED, outcome.result)
        if outcome.status is AnalysisServiceStatus.ALREADY_PROCESSING:
            return MonetizedAnalysisResult(MonetizedStatus.ALREADY_PROCESSING)
        await self._previews.release_preview(user_id, analysis_id)
        return MonetizedAnalysisResult(self._map_terminal(outcome.status))

    async def run_full(self, analysis_id: UUID, user_id: UUID) -> MonetizedAnalysisResult:
        spent = await self._credits.spend(user_id, analysis_id, self._price)
        if spent.outcome is SpendOutcome.INSUFFICIENT_BALANCE:
            return MonetizedAnalysisResult(
                MonetizedStatus.INSUFFICIENT_CREDITS, balance=spent.balance
            )
        if (
            spent.outcome not in {SpendOutcome.SPENT, SpendOutcome.ALREADY_SPENT}
            or spent.transaction_id is None
        ):
            return MonetizedAnalysisResult(MonetizedStatus.NOT_FOUND)
        outcome = await self._analysis.analyze(analysis_id, user_id)
        if outcome.status is AnalysisServiceStatus.COMPLETED and outcome.result is not None:
            await self._set_access(analysis_id, user_id, "full", self._price, spent.transaction_id)
            return MonetizedAnalysisResult(MonetizedStatus.FULL_COMPLETED, outcome.result)
        if outcome.status is AnalysisServiceStatus.ALREADY_PROCESSING:
            return MonetizedAnalysisResult(MonetizedStatus.ALREADY_PROCESSING)
        refund = await self._credits.refund(spent.transaction_id)
        status = (
            MonetizedStatus.TECHNICAL_FAILURE_REFUNDED
            if refund is RefundOutcome.REFUNDED
            else MonetizedStatus.TECHNICAL_FAILURE_ALREADY_REFUNDED
        )
        return MonetizedAnalysisResult(status)

    async def unlock_full(self, analysis_id: UUID, user_id: UUID) -> MonetizedAnalysisResult:
        async with self._sessions() as session:
            analysis = await session.scalar(
                select(Analysis).where(Analysis.id == analysis_id, Analysis.user_id == user_id)
            )
            if analysis is None:
                return MonetizedAnalysisResult(MonetizedStatus.NOT_FOUND)
            if analysis.status == "deleted":
                return MonetizedAnalysisResult(MonetizedStatus.DELETED)
            if analysis.status != "completed" or analysis.report_access not in {"preview", "full"}:
                return MonetizedAnalysisResult(MonetizedStatus.NOT_READY)
            try:
                result = AnalysisResult.model_validate_json(json.dumps(analysis.result_json))
            except (ValidationError, ValueError, TypeError):
                return MonetizedAnalysisResult(MonetizedStatus.CORRUPTED_RESULT)
            if analysis.report_access == "full":
                return MonetizedAnalysisResult(MonetizedStatus.FULL_COMPLETED, result)
        spent = await self._credits.spend(user_id, analysis_id, self._price)
        if spent.outcome is SpendOutcome.INSUFFICIENT_BALANCE:
            return MonetizedAnalysisResult(
                MonetizedStatus.INSUFFICIENT_CREDITS, balance=spent.balance
            )
        if spent.transaction_id is None:
            return MonetizedAnalysisResult(MonetizedStatus.NOT_FOUND)
        await self._set_access(analysis_id, user_id, "full", self._price, spent.transaction_id)
        return MonetizedAnalysisResult(MonetizedStatus.FULL_COMPLETED, result)

    async def _set_access(
        self, analysis_id: UUID, user_id: UUID, access: str, cost: int, transaction_id: UUID | None
    ) -> None:
        async with self._sessions.begin() as session:
            analysis = await session.scalar(
                select(Analysis)
                .where(Analysis.id == analysis_id, Analysis.user_id == user_id)
                .with_for_update()
            )
            if analysis is None or analysis.status != "completed":
                return
            analysis.report_access, analysis.cost_units = access, cost
            analysis.full_access_transaction_id = transaction_id

    @staticmethod
    def _map_terminal(status: AnalysisServiceStatus) -> MonetizedStatus:
        if status is AnalysisServiceStatus.DELETED:
            return MonetizedStatus.DELETED
        if status is AnalysisServiceStatus.NOT_FOUND:
            return MonetizedStatus.NOT_FOUND
        return MonetizedStatus.NOT_READY
