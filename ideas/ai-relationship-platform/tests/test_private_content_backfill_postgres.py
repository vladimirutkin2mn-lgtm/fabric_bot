"""Restartable keyset backfill behavior on isolated PostgreSQL."""

from uuid import UUID

import pytest
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.cli.backfill_private_content import backfill_batch
from app.db.models import Analysis, AnalysisPrivateContent, User
from app.services.sensitive_content import AESGCMSensitiveContentCipher, ContentPurpose

pytestmark = pytest.mark.postgres


class FailingCipher(AESGCMSensitiveContentCipher):
    def encrypt_json(self, purpose: ContentPurpose, value: object) -> bytes:
        raise RuntimeError("safe encryption failure")


async def test_conflict_batch_does_not_block_three_later_batches(
    payment_db: async_sessionmaker[AsyncSession],
) -> None:
    cipher = AESGCMSensitiveContentCipher("backfill-postgres-test-key-material")
    async with payment_db.begin() as session:
        user = User(telegram_user_id=750001, first_name="Backfill")
        session.add(user)
        await session.flush()
        for value in range(1, 9):
            analysis = Analysis(
                id=UUID(int=value),
                user_id=user.id,
                status="draft",
                intake_step="complete",
                normalized_conversation_json=[{"id": value, "text": "private"}],
            )
            session.add(analysis)
            if value <= 2:
                session.add(
                    AnalysisPrivateContent(
                        analysis_id=analysis.id, source_ciphertext=b"existing-conflict"
                    )
                )
    last_seen: UUID | None = None
    examined = conflicts = 0
    while True:
        async with payment_db() as session:
            count, found, last_seen = await backfill_batch(
                session, cipher, batch_size=2, after_id=last_seen
            )
        examined += count
        conflicts += found
        if count < 2:
            break
    assert examined == 8 and conflicts == 2
    async with payment_db() as session:
        legacy_count = await session.scalar(
            select(func.count())
            .select_from(Analysis)
            .where(Analysis.normalized_conversation_json.is_not(None))
        )
        encrypted_count = await session.scalar(
            select(func.count())
            .select_from(AnalysisPrivateContent)
            .where(AnalysisPrivateContent.source_ciphertext.is_not(None))
        )
    assert legacy_count == 2 and encrypted_count == 8


async def test_backfill_dry_run_scans_all_batches_without_changes(
    payment_db: async_sessionmaker[AsyncSession],
) -> None:
    cipher = AESGCMSensitiveContentCipher("backfill-postgres-test-key-material")
    async with payment_db.begin() as session:
        user = User(telegram_user_id=750002, first_name="Backfill")
        session.add(user)
        await session.flush()
        session.add_all(
            Analysis(
                id=UUID(int=100 + value),
                user_id=user.id,
                status="draft",
                intake_step="complete",
                user_goal="private",
            )
            for value in range(7)
        )
    last_seen: UUID | None = None
    examined = 0
    while True:
        async with payment_db() as session:
            count, _, last_seen = await backfill_batch(
                session, cipher, batch_size=2, dry_run=True, after_id=last_seen
            )
        examined += count
        if count < 2:
            break
    assert examined == 7
    async with payment_db() as session:
        assert await session.scalar(select(func.count()).select_from(AnalysisPrivateContent)) == 0


async def test_source_and_result_backfill_are_atomic_and_repeat_is_noop(
    payment_db: async_sessionmaker[AsyncSession],
) -> None:
    cipher = AESGCMSensitiveContentCipher("backfill-postgres-test-key-material")
    async with payment_db.begin() as session:
        user = User(telegram_user_id=750003, first_name="Backfill")
        session.add(user)
        await session.flush()
        analysis = Analysis(
            user_id=user.id,
            status="completed",
            intake_step="complete",
            normalized_conversation_json=[{"text": "private source"}],
            result_json={"summary": "private result"},
            completed_at=user.created_at,
        )
        session.add(analysis)
        await session.flush()
        analysis_id = analysis.id
    async with payment_db() as session:
        assert (await backfill_batch(session, cipher))[0] == 1
    async with payment_db() as session:
        stored = await session.get(Analysis, analysis_id)
        private = await session.get(AnalysisPrivateContent, analysis_id)
        assert stored is not None
        assert stored.normalized_conversation_json is None and stored.result_json is None
        assert private is not None
        assert private.source_ciphertext is not None and private.result_ciphertext is not None
    async with payment_db() as session:
        assert (await backfill_batch(session, cipher))[0] == 0


async def test_encryption_failure_rolls_back_plaintext_and_ciphertext(
    payment_db: async_sessionmaker[AsyncSession],
) -> None:
    async with payment_db.begin() as session:
        user = User(telegram_user_id=750004, first_name="Backfill")
        session.add(user)
        await session.flush()
        analysis = Analysis(
            user_id=user.id,
            status="draft",
            intake_step="complete",
            user_goal="private goal",
        )
        session.add(analysis)
        await session.flush()
        analysis_id = analysis.id
    async with payment_db() as session:
        with pytest.raises(RuntimeError, match="safe encryption failure"):
            await backfill_batch(session, FailingCipher("failure-test-key-material"))
        await session.rollback()
    async with payment_db() as session:
        stored = await session.get(Analysis, analysis_id)
        private = await session.get(AnalysisPrivateContent, analysis_id)
        assert stored is not None and stored.user_goal == "private goal"
        assert private is None
