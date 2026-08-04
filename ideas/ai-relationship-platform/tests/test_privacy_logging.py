"""Regression tests for privacy-safe deletion diagnostics."""

import logging
from collections.abc import Mapping

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.db.models import Analysis, User
from app.services.data_deletion import DataDeletionOutcome, DataDeletionService
from app.services.sensitive_content import AESGCMSensitiveContentCipher, ContentPurpose


class RaisingAnalytics:
    async def track(
        self, user_id: str | None, event: str, properties: Mapping[str, str] | None = None
    ) -> None:
        raise RuntimeError("analytics unavailable")


@pytest.mark.postgres
async def test_analytics_failure_does_not_undo_or_duplicate_deletion(
    payment_db: async_sessionmaker[AsyncSession], caplog: pytest.LogCaptureFixture
) -> None:
    sentinel = "PRIVATE-DELETE-SENTINEL-9f13"
    async with payment_db.begin() as session:
        user = User(telegram_user_id=760001, first_name="Privacy")
        session.add(user)
        await session.flush()
        analysis = Analysis(
            user_id=user.id,
            status="draft",
            intake_step="complete",
            user_goal=sentinel,
        )
        session.add(analysis)
        await session.flush()
        analysis_id, user_id = analysis.id, user.id
    caplog.set_level(logging.WARNING)
    async with payment_db() as session:
        assert (
            await DataDeletionService(session, RaisingAnalytics()).delete_analysis(
                analysis_id, user_id
            )
            is DataDeletionOutcome.DELETED
        )
    async with payment_db() as session:
        assert (
            await DataDeletionService(session, RaisingAnalytics()).delete_analysis(
                analysis_id, user_id
            )
            is DataDeletionOutcome.ALREADY_DELETED
        )
        stored = await session.get(Analysis, analysis_id)
        assert stored is not None and stored.status == "deleted" and stored.user_goal is None
    assert sentinel not in caplog.text
    assert caplog.text.count("privacy_analytics_failed event=analysis_deleted") == 1


def test_cipher_repr_and_authentication_error_hide_sentinels() -> None:
    key = "PRIVATE-KEY-SENTINEL-with-enough-material"
    content = "PRIVATE-CONTENT-SENTINEL"
    cipher = AESGCMSensitiveContentCipher(key)
    ciphertext = cipher.encrypt_json(ContentPurpose.ANALYSIS_SOURCE, content)
    rendered = repr(cipher) + repr(ciphertext)
    assert key not in rendered and content not in rendered
