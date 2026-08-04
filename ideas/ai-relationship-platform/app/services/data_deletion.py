"""Transactional, idempotent privacy deletion without mutating the ledger."""

from datetime import UTC, datetime
from enum import StrEnum
from uuid import UUID

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import (
    Analysis,
    AnalysisPrivateContent,
    BillingCustomer,
    BillingJob,
    BillingOutboxEvent,
    PaymentOrder,
    Subscription,
    User,
)
from app.providers.analytics import AnalyticsClient


class DataDeletionOutcome(StrEnum):
    DELETED = "deleted"
    ALREADY_DELETED = "already_deleted"
    NOT_FOUND = "not_found"


class DataDeletionService:
    def __init__(self, session: AsyncSession, analytics: AnalyticsClient) -> None:
        self.session, self.analytics = session, analytics

    async def delete_analysis(self, analysis_id: UUID, user_id: UUID) -> DataDeletionOutcome:
        now = datetime.now(UTC)
        user = await self.session.scalar(select(User).where(User.id == user_id).with_for_update())
        analysis = await self.session.scalar(
            select(Analysis)
            .where(Analysis.id == analysis_id, Analysis.user_id == user_id)
            .with_for_update()
        )
        if user is None or analysis is None:
            await self.session.rollback()
            return DataDeletionOutcome.NOT_FOUND
        if analysis.status == "deleted":
            await self.session.commit()
            return DataDeletionOutcome.ALREADY_DELETED
        private = await self.session.get(AnalysisPrivateContent, analysis_id, with_for_update=True)
        if private:
            private.source_ciphertext = private.result_ciphertext = None
            private.source_deleted_at = private.result_deleted_at = now
        self._clear_analysis(analysis, now)
        if user.free_preview_status == "reserved" and user.free_preview_analysis_id == analysis.id:
            user.free_preview_status = "available"
            user.free_preview_analysis_id = None
        await self.session.commit()
        await self.analytics.track(None, "analysis_deleted", {"analysis_id": str(analysis_id)})
        return DataDeletionOutcome.DELETED

    async def delete_account(self, user_id: UUID) -> DataDeletionOutcome:
        now = datetime.now(UTC)
        user = await self.session.scalar(select(User).where(User.id == user_id).with_for_update())
        if user is None:
            return DataDeletionOutcome.NOT_FOUND
        if user.privacy_status == "deleted":
            await self.session.commit()
            return DataDeletionOutcome.ALREADY_DELETED
        analyses = list(
            (
                await self.session.scalars(
                    select(Analysis)
                    .where(Analysis.user_id == user_id)
                    .order_by(Analysis.id)
                    .with_for_update()
                )
            ).all()
        )
        ids = [row.id for row in analyses]
        if ids:
            private_rows = list(
                (
                    await self.session.scalars(
                        select(AnalysisPrivateContent)
                        .where(AnalysisPrivateContent.analysis_id.in_(ids))
                        .order_by(AnalysisPrivateContent.analysis_id)
                        .with_for_update()
                    )
                ).all()
            )
            for row in private_rows:
                row.source_ciphertext = row.result_ciphertext = None
                row.source_deleted_at = row.result_deleted_at = now
        for analysis in analyses:
            self._clear_analysis(analysis, now)
        await self.session.execute(
            update(PaymentOrder)
            .where(
                PaymentOrder.user_id == user_id,
                PaymentOrder.status.in_(("creating", "pending")),
            )
            .values(
                status="cancelled",
                checkout_url=None,
                encrypted_receipt_contact=None,
                commercial_snapshot={},
                failure_code="user_deleted",
                checkout_creation_attempt_id=None,
                checkout_creation_started_at=None,
            )
        )
        order_ids = list(
            await self.session.scalars(
                select(PaymentOrder.id).where(PaymentOrder.user_id == user_id)
            )
        )
        await self.session.execute(
            update(BillingCustomer)
            .where(BillingCustomer.user_id == user_id)
            .values(provider_customer_id=None)
        )
        await self.session.execute(
            update(Subscription)
            .where(Subscription.user_id == user_id)
            .values(
                status="canceled",
                encrypted_payment_method=None,
                canceled_at=now,
                renewal_claimed_by=None,
                renewal_lease_until=None,
            )
        )
        if order_ids:
            values = [str(value) for value in order_ids]
            await self.session.execute(
                update(BillingJob)
                .where(
                    BillingJob.object_id.in_(values),
                    BillingJob.status.in_(("pending", "claimed")),
                )
                .values(
                    status="manual_review",
                    last_error_code="user_deleted",
                    claimed_by=None,
                    claim_id=None,
                    lease_until=None,
                )
            )
            await self.session.execute(
                update(BillingOutboxEvent)
                .where(
                    BillingOutboxEvent.aggregate_id.in_(values),
                    BillingOutboxEvent.status.in_(("pending", "claimed")),
                )
                .values(
                    payload={},
                    status="manual_review",
                    last_error_code="user_deleted",
                    claimed_by=None,
                    claim_id=None,
                    lease_until=None,
                )
            )
        await self.session.execute(
            update(PaymentOrder)
            .where(PaymentOrder.user_id == user_id)
            .values(
                checkout_url=None,
                encrypted_receipt_contact=None,
            )
        )
        user.telegram_user_id = None
        user.telegram_username = user.first_name = user.telegram_language = None
        user.age_confirmed = False
        user.age_confirmed_at = user.consent_version = user.consent_accepted_at = None
        user.onboarding_completed = False
        user.free_preview_status = "available"
        user.free_preview_analysis_id = user.free_preview_used_at = None
        user.privacy_status, user.deleted_at = "deleted", now
        await self.session.commit()
        await self.analytics.track(None, "all_data_deleted", {})
        return DataDeletionOutcome.DELETED

    @staticmethod
    def _clear_analysis(analysis: Analysis, now: datetime) -> None:
        analysis.status, analysis.deleted_at = "deleted", now
        analysis.report_access = "none"
        analysis.normalized_conversation_json = analysis.participants_json = None
        analysis.user_participant_label = analysis.user_goal = analysis.relationship_stage = None
        analysis.result_json = None
        analysis.feedback_score = analysis.feedback_submitted_at = None
        analysis.message_count = analysis.character_count = 0
        analysis.completed_at = None
