"""Source-retention state transitions on isolated PostgreSQL."""

import asyncio
from datetime import UTC, datetime, timedelta
from uuid import UUID

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.db.models import Analysis, AnalysisPrivateContent, User
from app.providers.analytics import NoOpAnalyticsClient
from app.repositories.analyses import SqlAlchemyAnalysisRepository
from app.repositories.private_content import AnalysisSource, EncryptedAnalysisContentRepository
from app.services.data_deletion import DataDeletionOutcome, DataDeletionService
from app.services.report_renderer import ReportRenderer
from app.services.report_service import ReportService, ReportStatus
from app.services.retention import cleanup_expired_source
from app.services.sensitive_content import AESGCMSensitiveContentCipher
from tests.test_report_service import payload

pytestmark = pytest.mark.postgres


@pytest.mark.parametrize("status", ["draft", "queued", "processing"])
async def test_expired_incomplete_source_becomes_deleted(
    payment_db: async_sessionmaker[AsyncSession], status: str
) -> None:
    async with payment_db.begin() as session:
        user = User(telegram_user_id=740001, first_name="Retention")
        session.add(user)
        await session.flush()
        analysis = Analysis(user_id=user.id, status=status, intake_step="complete")
        session.add(analysis)
        await session.flush()
        session.add(
            AnalysisPrivateContent(
                analysis_id=analysis.id,
                source_ciphertext=b"expired",
                result_ciphertext=b"preserve-if-present",
                source_delete_after=datetime.now(UTC) - timedelta(seconds=1),
            )
        )
        analysis_id = analysis.id
    async with payment_db() as session:
        result = await cleanup_expired_source(session, batch_size=10)
        assert result.cleared == 1
    async with payment_db() as session:
        stored_analysis = await session.get(Analysis, analysis_id)
        private = await session.get(AnalysisPrivateContent, analysis_id)
        assert stored_analysis is not None and stored_analysis.status == "deleted"
        assert private is not None and private.source_ciphertext is None
        assert private.result_ciphertext == b"preserve-if-present"


async def test_retention_dry_run_and_repeat_are_noops(
    payment_db: async_sessionmaker[AsyncSession],
) -> None:
    async with payment_db.begin() as session:
        user = User(telegram_user_id=740002, first_name="Retention")
        session.add(user)
        await session.flush()
        analysis = Analysis(user_id=user.id, status="draft", intake_step="complete")
        session.add(analysis)
        await session.flush()
        session.add(
            AnalysisPrivateContent(
                analysis_id=analysis.id,
                source_ciphertext=b"expired",
                source_delete_after=datetime.now(UTC) - timedelta(seconds=1),
            )
        )
        analysis_id = analysis.id
    async with payment_db() as session:
        assert (await cleanup_expired_source(session, dry_run=True)).cleared == 0
    async with payment_db() as session:
        private = await session.get(AnalysisPrivateContent, analysis_id)
        assert private is not None and private.source_ciphertext == b"expired"
    async with payment_db() as session:
        assert (await cleanup_expired_source(session)).cleared == 1
    async with payment_db() as session:
        assert (await cleanup_expired_source(session)).cleared == 0


async def test_two_skip_locked_workers_partition_expired_rows(
    payment_db: async_sessionmaker[AsyncSession],
) -> None:
    async with payment_db.begin() as session:
        user = User(telegram_user_id=740003, first_name="Retention")
        session.add(user)
        await session.flush()
        for _ in range(20):
            analysis = Analysis(user_id=user.id, status="draft", intake_step="complete")
            session.add(analysis)
            await session.flush()
            session.add(
                AnalysisPrivateContent(
                    analysis_id=analysis.id,
                    source_ciphertext=b"expired",
                    source_delete_after=datetime.now(UTC) - timedelta(seconds=1),
                )
            )

    first_selected = asyncio.Event()
    second_selected = asyncio.Event()
    batches: list[tuple[object, ...]] = []

    async def first_hook(ids: tuple[object, ...]) -> None:
        batches.append(ids)
        first_selected.set()
        await asyncio.wait_for(second_selected.wait(), timeout=5)

    async def second_hook(ids: tuple[object, ...]) -> None:
        batches.append(ids)
        second_selected.set()

    async def first_worker() -> int:
        async with payment_db() as session:
            return (
                await cleanup_expired_source(session, batch_size=10, after_lock=first_hook)
            ).cleared

    async def second_worker() -> int:
        await asyncio.wait_for(first_selected.wait(), timeout=5)
        async with payment_db() as session:
            return (
                await cleanup_expired_source(session, batch_size=10, after_lock=second_hook)
            ).cleared

    cleared = await asyncio.gather(first_worker(), second_worker())
    assert len(batches) == 2 and all(batches)
    assert set(batches[0]).isdisjoint(batches[1])
    assert sum(cleared) == 20
    async with payment_db() as session:
        assert (await cleanup_expired_source(session, batch_size=100)).cleared == 0


async def test_completed_encrypted_report_survives_source_retention(
    payment_db: async_sessionmaker[AsyncSession],
) -> None:
    cipher = AESGCMSensitiveContentCipher("retention-report-test-key-material")
    async with payment_db.begin() as session:
        user = User(telegram_user_id=740004, first_name="Retention")
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
        content = EncryptedAnalysisContentRepository(session, cipher, 30)
        await content.store_source(
            analysis.id,
            AnalysisSource([], {"A": "One", "B": "Two"}, "A", "Goal", "dating"),
            replace=True,
        )
        assert await content.store_result(analysis.id, payload())
        private = await session.get(AnalysisPrivateContent, analysis.id)
        assert private is not None
        private.source_delete_after = datetime.now(UTC) - timedelta(seconds=1)
        analysis_id, user_id = analysis.id, user.id
    async with payment_db() as session:
        assert (await cleanup_expired_source(session)).cleared == 1
    async with payment_db() as session:
        repository = SqlAlchemyAnalysisRepository(session, cipher, 30)
        reports = ReportService(repository, ReportRenderer(), NoOpAnalyticsClient())
        preview = await reports.retrieve(analysis_id, user_id)
        assert preview.status is ReportStatus.COMPLETED and preview.report is not None
        history = await reports.history(user_id, 0)
        assert [item.analysis_id for item in history.items] == [analysis_id]
        stored = await session.get(Analysis, analysis_id)
        private = await session.get(AnalysisPrivateContent, analysis_id)
        assert stored is not None and stored.result_json is None
        assert private is not None and private.source_ciphertext is None
        assert private.result_ciphertext is not None
        stored.report_access = "full"
        await session.commit()
    async with payment_db() as session:
        full = await ReportService(
            SqlAlchemyAnalysisRepository(session, cipher, 30),
            ReportRenderer(),
            NoOpAnalyticsClient(),
        ).retrieve(analysis_id, user_id)
        assert full.status is ReportStatus.COMPLETED and full.report is not None


async def test_context_updates_preserve_original_source_deadline(
    payment_db: async_sessionmaker[AsyncSession],
) -> None:
    cipher = AESGCMSensitiveContentCipher("retention-context-test-key-material")
    async with payment_db.begin() as session:
        user = User(telegram_user_id=740005, first_name="Retention")
        session.add(user)
        await session.flush()
        analysis = Analysis(user_id=user.id, status="draft", intake_step="complete")
        session.add(analysis)
        await session.flush()
        content = EncryptedAnalysisContentRepository(session, cipher, 30)
        await content.store_source(
            analysis.id, AnalysisSource([], {"A": "One", "B": "Two"}), replace=True
        )
        private = await session.get(AnalysisPrivateContent, analysis.id)
        assert private is not None and private.source_delete_after is not None
        original = private.source_delete_after
        await content.store_source(
            analysis.id,
            AnalysisSource([], {"A": "One", "B": "Two"}, "A", "Updated goal", "dating"),
        )
        await session.flush()
        assert private.source_delete_after == original


@pytest.mark.parametrize("first", ["retention", "deletion"])
async def test_account_deletion_and_retention_overlap_25_times(
    payment_db: async_sessionmaker[AsyncSession],
    caplog: pytest.LogCaptureFixture,
    first: str,
) -> None:
    for iteration in range(25):
        sentinel = f"PRIVATE-RETENTION-RACE-{first}-{iteration}"
        async with payment_db.begin() as session:
            user = User(
                telegram_user_id=(741000 if first == "retention" else 742000) + iteration,
                first_name="Retention race",
            )
            session.add(user)
            await session.flush()
            analysis = Analysis(
                user_id=user.id,
                status="completed",
                intake_step="complete",
                completed_at=datetime.now(UTC),
                report_access="full",
                normalized_conversation_json=[{"text": sentinel}],
                result_json={"summary": sentinel},
            )
            session.add(analysis)
            await session.flush()
            session.add(
                AnalysisPrivateContent(
                    analysis_id=analysis.id,
                    source_ciphertext=sentinel.encode(),
                    result_ciphertext=sentinel.encode(),
                    source_delete_after=datetime.now(UTC) - timedelta(seconds=1),
                )
            )
            user_id, analysis_id = user.id, analysis.id

        retention_locked = asyncio.Event()
        deletion_locked_user = asyncio.Event()
        release_retention = asyncio.Event()

        async def after_retention_lock(
            _ids: tuple[object, ...],
            locked: asyncio.Event = retention_locked,
            deletion_locked: asyncio.Event = deletion_locked_user,
            release: asyncio.Event = release_retention,
        ) -> None:
            locked.set()
            if first == "retention":
                await asyncio.wait_for(deletion_locked.wait(), timeout=5)
            else:
                await asyncio.wait_for(release.wait(), timeout=5)

        async def after_deletion_user_lock(
            deletion_locked: asyncio.Event = deletion_locked_user,
            locked: asyncio.Event = retention_locked,
            release: asyncio.Event = release_retention,
        ) -> None:
            deletion_locked.set()
            if first == "deletion":
                await asyncio.wait_for(locked.wait(), timeout=5)
                release.set()

        async def retain(deletion_locked: asyncio.Event = deletion_locked_user) -> int:
            if first == "deletion":
                await asyncio.wait_for(deletion_locked.wait(), timeout=5)
            async with payment_db() as session:
                return (
                    await cleanup_expired_source(
                        session,
                        batch_size=1,
                        after_lock=after_retention_lock,
                    )
                ).cleared

        async def delete(
            locked: asyncio.Event = retention_locked,
            target_user_id: UUID = user_id,
        ) -> DataDeletionOutcome:
            if first == "retention":
                await asyncio.wait_for(locked.wait(), timeout=5)
            async with payment_db() as session:
                return await DataDeletionService(
                    session,
                    NoOpAnalyticsClient(),
                    _after_user_lock_for_test=after_deletion_user_lock,
                ).delete_account(target_user_id)

        retained, deleted = await asyncio.wait_for(asyncio.gather(retain(), delete()), timeout=10)
        assert retained in {0, 1}
        assert deleted is DataDeletionOutcome.DELETED
        async with payment_db() as session:
            stored_user = await session.get(User, user_id)
            stored = await session.get(Analysis, analysis_id)
            private = await session.get(AnalysisPrivateContent, analysis_id)
            assert stored_user is not None and stored_user.privacy_status == "deleted"
            assert stored is not None and stored.status == "deleted"
            assert stored.normalized_conversation_json is None and stored.result_json is None
            assert private is not None
            assert private.source_ciphertext is None and private.result_ciphertext is None
            assert (await cleanup_expired_source(session, batch_size=1)).cleared == 0
        assert sentinel not in caplog.text
