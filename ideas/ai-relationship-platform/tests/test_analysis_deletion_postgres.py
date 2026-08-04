"""Single-analysis privacy deletion invariants on isolated PostgreSQL."""

import asyncio
from datetime import UTC, datetime
from uuid import UUID

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.db.models import Analysis, AnalysisPrivateContent, CreditTransaction, User
from app.providers.analytics import NoOpAnalyticsClient
from app.repositories.analyses import LLMMetadata, SqlAlchemyAnalysisRepository
from app.services.data_deletion import DataDeletionOutcome, DataDeletionService
from app.services.report_renderer import ReportRenderer
from app.services.report_service import ReportService, ReportStatus
from app.services.sensitive_content import AESGCMSensitiveContentCipher

pytestmark = pytest.mark.postgres


@pytest.mark.parametrize("status", ["draft", "queued", "processing", "completed", "failed"])
async def test_delete_analysis_clears_all_content_for_every_state(
    payment_db: async_sessionmaker[AsyncSession], status: str
) -> None:
    async with payment_db.begin() as session:
        user = User(telegram_user_id=730001, first_name="Owner")
        session.add(user)
        await session.flush()
        analysis = Analysis(
            user_id=user.id,
            status=status,
            intake_step="complete",
            normalized_conversation_json=[{"private": "source"}],
            participants_json={"A": "private"},
            user_participant_label="A",
            user_goal="private goal",
            relationship_stage="dating",
            result_json={"private": "result"} if status == "completed" else None,
            completed_at=datetime.now(UTC) if status == "completed" else None,
            failure_code="safe_failure" if status == "failed" else None,
            report_access="full" if status == "completed" else "none",
            feedback_score=5 if status == "completed" else None,
            feedback_submitted_at=datetime.now(UTC) if status == "completed" else None,
        )
        session.add(analysis)
        await session.flush()
        session.add(
            AnalysisPrivateContent(
                analysis_id=analysis.id,
                source_ciphertext=b"source-ciphertext",
                result_ciphertext=b"result-ciphertext",
            )
        )
        analysis_id, user_id = analysis.id, user.id
    async with payment_db() as session:
        outcome = await DataDeletionService(session, NoOpAnalyticsClient()).delete_analysis(
            analysis_id, user_id
        )
        assert outcome is DataDeletionOutcome.DELETED
    async with payment_db() as session:
        stored_analysis = await session.get(Analysis, analysis_id)
        private = await session.get(AnalysisPrivateContent, analysis_id)
        assert stored_analysis is not None and stored_analysis.status == "deleted"
        assert stored_analysis.deleted_at is not None and stored_analysis.report_access == "none"
        assert (
            stored_analysis.feedback_score is None and stored_analysis.feedback_submitted_at is None
        )
        assert stored_analysis.normalized_conversation_json is None
        assert stored_analysis.result_json is None
        assert stored_analysis.user_goal is None and stored_analysis.participants_json is None
        assert private is not None
        assert private.source_ciphertext is None and private.result_ciphertext is None


async def test_delete_analysis_is_idempotent_and_enforces_owner(
    payment_db: async_sessionmaker[AsyncSession],
) -> None:
    async with payment_db.begin() as session:
        owner = User(telegram_user_id=730002, first_name="Owner")
        stranger = User(telegram_user_id=730003, first_name="Stranger")
        session.add_all((owner, stranger))
        await session.flush()
        analysis = Analysis(user_id=owner.id, status="draft", intake_step="complete")
        session.add(analysis)
        await session.flush()
        analysis_id, owner_id, stranger_id = analysis.id, owner.id, stranger.id
    async with payment_db() as session:
        service = DataDeletionService(session, NoOpAnalyticsClient())
        assert (
            await service.delete_analysis(analysis_id, stranger_id) is DataDeletionOutcome.NOT_FOUND
        )
    async with payment_db() as session:
        service = DataDeletionService(session, NoOpAnalyticsClient())
        assert await service.delete_analysis(analysis_id, owner_id) is DataDeletionOutcome.DELETED
    async with payment_db() as session:
        service = DataDeletionService(session, NoOpAnalyticsClient())
        assert (
            await service.delete_analysis(analysis_id, owner_id)
            is DataDeletionOutcome.ALREADY_DELETED
        )


async def test_simultaneous_deletion_releases_reserved_preview_and_blocks_report(
    payment_db: async_sessionmaker[AsyncSession],
) -> None:
    async with payment_db.begin() as session:
        user = User(telegram_user_id=730004, first_name="Owner")
        session.add(user)
        await session.flush()
        analysis = Analysis(
            user_id=user.id,
            status="completed",
            intake_step="complete",
            completed_at=datetime.now(UTC),
            report_access="preview",
        )
        session.add(analysis)
        await session.flush()
        user.free_preview_status = "reserved"
        user.free_preview_analysis_id = analysis.id
        session.add(
            AnalysisPrivateContent(
                analysis_id=analysis.id,
                source_ciphertext=b"private-source",
                result_ciphertext=b"private-result",
            )
        )
        analysis_id, user_id = analysis.id, user.id

    async def delete() -> DataDeletionOutcome:
        async with payment_db() as session:
            return await DataDeletionService(session, NoOpAnalyticsClient()).delete_analysis(
                analysis_id, user_id
            )

    outcomes = await asyncio.wait_for(asyncio.gather(delete(), delete()), timeout=5)
    assert set(outcomes) == {
        DataDeletionOutcome.DELETED,
        DataDeletionOutcome.ALREADY_DELETED,
    }
    async with payment_db() as session:
        stored_user = await session.get(User, user_id)
        assert stored_user is not None
        assert stored_user.free_preview_status == "available"
        assert stored_user.free_preview_analysis_id is None
        report = await ReportService(
            SqlAlchemyAnalysisRepository(session),
            ReportRenderer(),
            NoOpAnalyticsClient(),
        ).retrieve(analysis_id, user_id)
        assert report.status is ReportStatus.DELETED


async def test_deletion_preserves_consumed_preview_and_financial_references(
    payment_db: async_sessionmaker[AsyncSession],
) -> None:
    consumed_at = datetime.now(UTC)
    async with payment_db.begin() as session:
        user = User(
            telegram_user_id=730005,
            first_name="Owner",
            free_preview_status="consumed",
            free_preview_used_at=consumed_at,
        )
        session.add(user)
        await session.flush()
        analysis = Analysis(user_id=user.id, status="processing", intake_step="complete")
        session.add(analysis)
        await session.flush()
        transaction = CreditTransaction(
            user_id=user.id,
            type="spend",
            amount=-1,
            idempotency_key=f"analysis-delete:{analysis.id}",
            analysis_id=analysis.id,
        )
        session.add(transaction)
        await session.flush()
        analysis.cost_units = 1
        analysis.full_access_transaction_id = transaction.id
        session.add(
            AnalysisPrivateContent(
                analysis_id=analysis.id,
                source_ciphertext=b"private-source",
            )
        )
        analysis_id, user_id, transaction_id = analysis.id, user.id, transaction.id

    async with payment_db() as session:
        outcome = await DataDeletionService(session, NoOpAnalyticsClient()).delete_analysis(
            analysis_id, user_id
        )
        assert outcome is DataDeletionOutcome.DELETED
    async with payment_db() as session:
        stored_user = await session.get(User, user_id)
        stored_analysis = await session.get(Analysis, analysis_id)
        stored_transaction = await session.get(CreditTransaction, transaction_id)
        assert stored_user is not None
        assert stored_user.free_preview_status == "consumed"
        assert stored_user.free_preview_used_at == consumed_at
        assert stored_analysis is not None
        assert stored_analysis.full_access_transaction_id == transaction_id
        assert stored_analysis.cost_units == 1
        assert stored_transaction is not None
        assert (
            stored_transaction.user_id,
            stored_transaction.type,
            stored_transaction.amount,
            stored_transaction.analysis_id,
        ) == (user_id, "spend", -1, analysis_id)


@pytest.mark.parametrize("winner", ["deletion", "completion"])
async def test_analysis_completion_and_deletion_forced_interleavings_25_times(
    payment_db: async_sessionmaker[AsyncSession], winner: str
) -> None:
    cipher = AESGCMSensitiveContentCipher("analysis-deletion-race-test-key-material")
    metadata = LLMMetadata("stub", "stub", "analysis_v1", 1)
    for iteration in range(25):
        async with payment_db.begin() as session:
            user = User(telegram_user_id=731000 + iteration, first_name="Race")
            session.add(user)
            await session.flush()
            analysis = Analysis(
                user_id=user.id,
                status="processing",
                intake_step="complete",
                report_access="none",
            )
            session.add(analysis)
            await session.flush()
            session.add(
                AnalysisPrivateContent(analysis_id=analysis.id, source_ciphertext=b"private-source")
            )
            analysis_id, user_id = analysis.id, user.id
        first_committed = asyncio.Event()

        async def delete(
            gate: asyncio.Event = first_committed,
            target_analysis: UUID = analysis_id,
            target_user: UUID = user_id,
        ) -> DataDeletionOutcome:
            if winner == "completion":
                await asyncio.wait_for(gate.wait(), timeout=5)
            async with payment_db() as session:
                outcome = await DataDeletionService(session, NoOpAnalyticsClient()).delete_analysis(
                    target_analysis, target_user
                )
            if winner == "deletion":
                gate.set()
            return outcome

        async def complete(
            gate: asyncio.Event = first_committed,
            target_analysis: UUID = analysis_id,
        ) -> str:
            if winner == "deletion":
                await asyncio.wait_for(gate.wait(), timeout=5)
            try:
                async with payment_db() as session:
                    await SqlAlchemyAnalysisRepository(session, cipher, 30).complete_processing(
                        target_analysis, {"summary": "private-result"}, metadata
                    )
                outcome = "completed"
            except RuntimeError:
                outcome = "lost"
            if winner == "completion":
                gate.set()
            return outcome

        deletion, completion = await asyncio.gather(delete(), complete())
        assert deletion is DataDeletionOutcome.DELETED
        assert completion == ("lost" if winner == "deletion" else "completed")
        async with payment_db() as session:
            stored = await session.get(Analysis, analysis_id)
            private = await session.get(AnalysisPrivateContent, analysis_id)
            assert stored is not None and stored.status == "deleted"
            assert private is not None
            assert private.source_ciphertext is None and private.result_ciphertext is None
