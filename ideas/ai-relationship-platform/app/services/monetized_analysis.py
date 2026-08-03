"""Financial orchestration around the existing analysis pipeline."""

import asyncio
import json
from dataclasses import dataclass
from enum import StrEnum
from uuid import UUID

from pydantic import ValidationError
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.db.models import Analysis, CreditTransaction
from app.domain.analysis import AnalysisResult
from app.providers.analytics import AnalyticsClient
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
    TECHNICAL_FAILURE_REFUND_FAILED = "technical_failure_refund_failed"


class AccessOutcome(StrEnum):
    UPDATED = "updated"
    ALREADY_FULL_SAME_TRANSACTION = "already_full_same_transaction"
    NOT_FOUND = "not_found"
    DELETED = "deleted"
    NOT_COMPLETED = "not_completed"
    TRANSACTION_MISMATCH = "transaction_mismatch"
    ACCESS_CONFLICT = "access_conflict"


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
        analytics: AnalyticsClient,
    ) -> None:
        self._sessions, self._credits, self._previews = sessions, credits, previews
        self._analysis, self._price = analysis, analysis_price
        self._analytics = analytics

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
        try:
            outcome = await self._analysis.analyze(analysis_id, user_id)
            if outcome.status is AnalysisServiceStatus.COMPLETED and outcome.result is not None:
                consumed = await self._previews.finalize_preview(user_id, analysis_id)
                if consumed is not PreviewOutcome.CONSUMED:
                    await self._previews.release_preview(user_id, analysis_id)
                    return MonetizedAnalysisResult(MonetizedStatus.NOT_READY)
                await self._track(user_id, "preview_viewed", analysis_id)
                return MonetizedAnalysisResult(MonetizedStatus.PREVIEW_COMPLETED, outcome.result)
            if outcome.status is AnalysisServiceStatus.ALREADY_PROCESSING:
                return MonetizedAnalysisResult(MonetizedStatus.ALREADY_PROCESSING)
            await self._previews.release_preview(user_id, analysis_id)
            return MonetizedAnalysisResult(self._map_terminal(outcome.status))
        except asyncio.CancelledError:
            try:
                await self._previews.release_preview(user_id, analysis_id)
            finally:
                raise
        except Exception:
            await self._previews.release_preview(user_id, analysis_id)
            return MonetizedAnalysisResult(MonetizedStatus.NOT_READY)

    async def run_full(self, analysis_id: UUID, user_id: UUID) -> MonetizedAnalysisResult:
        spent = await self._credits.spend(user_id, analysis_id, self._price)
        if spent.outcome is SpendOutcome.INSUFFICIENT_BALANCE:
            await self._track(user_id, "paywall_viewed", analysis_id)
            return MonetizedAnalysisResult(
                MonetizedStatus.INSUFFICIENT_CREDITS, balance=spent.balance
            )
        if (
            spent.outcome not in {SpendOutcome.SPENT, SpendOutcome.ALREADY_SPENT}
            or spent.transaction_id is None
        ):
            return MonetizedAnalysisResult(MonetizedStatus.NOT_FOUND)
        try:
            if spent.outcome is SpendOutcome.SPENT:
                await self._track(user_id, "credit_spent", analysis_id)
            await self._track(user_id, "analysis_started", analysis_id)
            outcome = await self._analysis.analyze(analysis_id, user_id)
            if outcome.status is AnalysisServiceStatus.COMPLETED and outcome.result is not None:
                access = await self._set_access(
                    analysis_id, user_id, "full", self._price, spent.transaction_id
                )
                if access in {AccessOutcome.UPDATED, AccessOutcome.ALREADY_FULL_SAME_TRANSACTION}:
                    return MonetizedAnalysisResult(MonetizedStatus.FULL_COMPLETED, outcome.result)
                return await self._refund_result(
                    user_id, analysis_id, spent.transaction_id, outcome.result
                )
            if outcome.status is AnalysisServiceStatus.ALREADY_PROCESSING:
                return MonetizedAnalysisResult(MonetizedStatus.ALREADY_PROCESSING)
            return await self._refund_result(user_id, analysis_id, spent.transaction_id)
        except asyncio.CancelledError:
            try:
                await self._credits.refund_if_not_full(
                    user_id, analysis_id, spent.transaction_id, self._price
                )
            finally:
                raise
        except Exception:
            return await self._refund_result(user_id, analysis_id, spent.transaction_id)

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
            await self._track(user_id, "paywall_viewed", analysis_id)
            return MonetizedAnalysisResult(
                MonetizedStatus.INSUFFICIENT_CREDITS, balance=spent.balance
            )
        if spent.transaction_id is None:
            return MonetizedAnalysisResult(MonetizedStatus.NOT_FOUND)
        try:
            access = await self._set_access(
                analysis_id, user_id, "full", self._price, spent.transaction_id
            )
        except asyncio.CancelledError:
            try:
                await self._credits.refund_if_not_full(
                    user_id, analysis_id, spent.transaction_id, self._price
                )
            finally:
                raise
        except Exception:
            return await self._refund_result(user_id, analysis_id, spent.transaction_id, result)
        if access not in {AccessOutcome.UPDATED, AccessOutcome.ALREADY_FULL_SAME_TRANSACTION}:
            return await self._refund_result(user_id, analysis_id, spent.transaction_id, result)
        return MonetizedAnalysisResult(MonetizedStatus.FULL_COMPLETED, result)

    async def _set_access(
        self, analysis_id: UUID, user_id: UUID, access: str, cost: int, transaction_id: UUID | None
    ) -> AccessOutcome:
        async with self._sessions.begin() as session:
            analysis = await session.scalar(
                select(Analysis)
                .where(Analysis.id == analysis_id, Analysis.user_id == user_id)
                .with_for_update()
            )
            if analysis is None:
                return AccessOutcome.NOT_FOUND
            if analysis.status == "deleted":
                return AccessOutcome.DELETED
            if analysis.status != "completed":
                return AccessOutcome.NOT_COMPLETED
            if access == "full":
                if transaction_id is None:
                    return AccessOutcome.TRANSACTION_MISMATCH
                spend = await session.scalar(
                    select(CreditTransaction)
                    .where(CreditTransaction.id == transaction_id)
                    .with_for_update()
                )
                if (
                    spend is None
                    or spend.user_id != user_id
                    or spend.analysis_id != analysis_id
                    or spend.type != "spend"
                    or spend.amount != -cost
                ):
                    return AccessOutcome.TRANSACTION_MISMATCH
                refund = await session.scalar(
                    select(CreditTransaction.id)
                    .where(CreditTransaction.reverses_transaction_id == spend.id)
                    .with_for_update()
                )
                if refund is not None:
                    return AccessOutcome.TRANSACTION_MISMATCH
            if analysis.report_access == "full":
                return (
                    AccessOutcome.ALREADY_FULL_SAME_TRANSACTION
                    if analysis.full_access_transaction_id == transaction_id
                    and analysis.cost_units == cost
                    else AccessOutcome.TRANSACTION_MISMATCH
                )
            if access == "full" and analysis.report_access not in {"none", "preview"}:
                return AccessOutcome.ACCESS_CONFLICT
            analysis.report_access, analysis.cost_units = access, cost
            analysis.full_access_transaction_id = transaction_id
            await session.flush()
            if (
                analysis.report_access != access
                or analysis.cost_units != cost
                or analysis.full_access_transaction_id != transaction_id
            ):
                return AccessOutcome.ACCESS_CONFLICT
        return AccessOutcome.UPDATED

    async def _refund_result(
        self,
        user_id: UUID,
        analysis_id: UUID,
        spend_id: UUID,
        result: AnalysisResult | None = None,
    ) -> MonetizedAnalysisResult:
        refund = await self._credits.refund_if_not_full(user_id, analysis_id, spend_id, self._price)
        if refund is RefundOutcome.ACCESS_ALREADY_GRANTED:
            return MonetizedAnalysisResult(MonetizedStatus.FULL_COMPLETED, result)
        if refund is RefundOutcome.REFUNDED:
            status = MonetizedStatus.TECHNICAL_FAILURE_REFUNDED
            await self._track(user_id, "credit_refunded", analysis_id)
        elif refund is RefundOutcome.ALREADY_REFUNDED:
            status = MonetizedStatus.TECHNICAL_FAILURE_ALREADY_REFUNDED
        else:
            status = MonetizedStatus.TECHNICAL_FAILURE_REFUND_FAILED
        return MonetizedAnalysisResult(status)

    async def _track(self, user_id: UUID, event: str, analysis_id: UUID) -> None:
        try:
            await self._analytics.track(str(user_id), event, {"analysis_id": str(analysis_id)})
        except Exception:
            return

    @staticmethod
    def _map_terminal(status: AnalysisServiceStatus) -> MonetizedStatus:
        if status is AnalysisServiceStatus.DELETED:
            return MonetizedStatus.DELETED
        if status is AnalysisServiceStatus.NOT_FOUND:
            return MonetizedStatus.NOT_FOUND
        return MonetizedStatus.NOT_READY
