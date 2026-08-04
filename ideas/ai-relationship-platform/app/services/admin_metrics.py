"""Aggregate operational and funnel metrics without returning row-level data."""

from datetime import UTC, datetime
from typing import Any

from pydantic import BaseModel, Field
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.db.analytics import AnalyticsEvent
from app.db.models import Analysis, BillingJob, BillingOutboxEvent, CreditTransaction

_REQUIRED_FUNNEL_EVENTS = (
    "bot_started",
    "onboarding_completed",
    "analysis_started",
    "conversation_submitted",
    "conversation_rejected",
    "preview_viewed",
    "paywall_viewed",
    "checkout_started",
    "purchase_completed",
    "analysis_processing_started",
    "analysis_completed",
    "analysis_failed",
    "reply_suggestions_requested",
    "followup_requested",
    "analysis_deleted",
    "all_data_deleted",
)


class ModelUsageMetrics(BaseModel):
    average_latency_ms: float | None = None
    average_input_tokens: float | None = None
    average_output_tokens: float | None = None
    average_total_tokens: float | None = None
    average_cost_units: float | None = None


class PurchaseMetrics(BaseModel):
    transaction_count: int = 0
    purchased_credit_total: int = 0


class FailureMetrics(BaseModel):
    user_validation_total: int = 0
    technical_total: int = 0
    conversation_rejection_reasons: dict[str, int] = Field(default_factory=dict)
    analysis_failure_codes: dict[str, int] = Field(default_factory=dict)


class AdminMetrics(BaseModel):
    generated_at: datetime
    analyses_by_status: dict[str, int]
    terminal_completed: int
    terminal_failed: int
    completion_rate: float | None
    model_usage: ModelUsageMetrics
    purchases: PurchaseMetrics
    funnel_events: dict[str, int]
    failures: FailureMetrics
    billing_jobs_by_status: dict[str, int]
    billing_outbox_by_status: dict[str, int]


class AdminMetricsService:
    """Read aggregate product and operational state from PostgreSQL."""

    def __init__(self, sessions: async_sessionmaker[AsyncSession]) -> None:
        self._sessions = sessions

    async def snapshot(self) -> AdminMetrics:
        async with self._sessions() as session:
            analyses_by_status = await _group_counts(session, Analysis.status, Analysis)
            completed = analyses_by_status.get("completed", 0)
            failed = analyses_by_status.get("failed", 0)
            terminal = completed + failed

            averages = (
                await session.execute(
                    select(
                        func.avg(Analysis.latency_ms),
                        func.avg(Analysis.input_tokens),
                        func.avg(Analysis.output_tokens),
                        func.avg(
                            func.coalesce(Analysis.input_tokens, 0)
                            + func.coalesce(Analysis.output_tokens, 0)
                        ),
                        func.avg(Analysis.cost_units),
                    ).where(Analysis.status.in_(("completed", "failed")))
                )
            ).one()

            purchase_count, purchase_total = (
                await session.execute(
                    select(
                        func.count(), func.coalesce(func.sum(CreditTransaction.amount), 0)
                    ).where(CreditTransaction.type == "purchase")
                )
            ).one()

            funnel: dict[str, int] = {name: 0 for name in _REQUIRED_FUNNEL_EVENTS}
            funnel.update(await _group_counts(session, AnalyticsEvent.event_name, AnalyticsEvent))

            rejection_reason = AnalyticsEvent.properties["rejection_reason"].astext
            rejection_reasons = await _group_counts(
                session,
                rejection_reason,
                AnalyticsEvent,
                AnalyticsEvent.event_name == "conversation_rejected",
            )
            failure_codes = await _group_counts(
                session,
                Analysis.failure_code,
                Analysis,
                Analysis.status == "failed",
                Analysis.failure_code.is_not(None),
            )
            jobs = await _group_counts(session, BillingJob.status, BillingJob)
            outbox = await _group_counts(session, BillingOutboxEvent.status, BillingOutboxEvent)

        return AdminMetrics(
            generated_at=datetime.now(UTC),
            analyses_by_status=analyses_by_status,
            terminal_completed=completed,
            terminal_failed=failed,
            completion_rate=(completed / terminal) if terminal else None,
            model_usage=ModelUsageMetrics(
                average_latency_ms=_float_or_none(averages[0]),
                average_input_tokens=_float_or_none(averages[1]),
                average_output_tokens=_float_or_none(averages[2]),
                average_total_tokens=_float_or_none(averages[3]),
                average_cost_units=_float_or_none(averages[4]),
            ),
            purchases=PurchaseMetrics(
                transaction_count=int(purchase_count),
                purchased_credit_total=int(purchase_total),
            ),
            funnel_events={name: int(funnel.get(name, 0)) for name in _REQUIRED_FUNNEL_EVENTS},
            failures=FailureMetrics(
                user_validation_total=sum(rejection_reasons.values()),
                technical_total=failed,
                conversation_rejection_reasons=rejection_reasons,
                analysis_failure_codes=failure_codes,
            ),
            billing_jobs_by_status=jobs,
            billing_outbox_by_status=outbox,
        )


async def _group_counts(
    session: AsyncSession,
    column: Any,
    source: Any,
    *conditions: Any,
) -> dict[str, int]:
    statement = select(column, func.count()).select_from(source).group_by(column)
    if conditions:
        statement = statement.where(*conditions)
    rows = (await session.execute(statement)).all()
    return {str(key): int(count) for key, count in rows if key is not None}


def _float_or_none(value: object) -> float | None:
    return None if value is None else float(value)
