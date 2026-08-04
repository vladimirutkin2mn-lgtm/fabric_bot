"""Actual aiogram privacy callbacks with MemoryStorage and a recording session."""

from collections.abc import AsyncGenerator, Callable
from datetime import UTC, datetime
from typing import cast
from uuid import UUID

import pytest
from aiogram.methods import SendMessage
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.bot import texts
from app.db.models import Analysis, CreditTransaction, User
from app.providers.analytics import NoOpAnalyticsClient
from app.repositories.analyses import SqlAlchemyAnalysisRepository
from app.repositories.private_content import AnalysisSource, EncryptedAnalysisContentRepository
from app.repositories.users import SqlAlchemyUserRepository
from app.services.data_deletion import DataDeletionOutcome, DataDeletionService
from app.services.onboarding import OnboardingService, TelegramIdentity
from app.services.report_renderer import ReportRenderer
from app.services.report_service import ReportService, ReportStatus
from app.services.sensitive_content import AESGCMSensitiveContentCipher
from tests.test_telegram_handlers import (
    Harness,
    callback_update,
    start_update,
)
from tests.test_telegram_handlers import (
    harness as _harness_fixture,
)


@pytest.fixture
async def privacy_harness() -> AsyncGenerator[Harness, None]:
    fixture_function = cast(
        Callable[[], AsyncGenerator[Harness, None]],
        _harness_fixture._fixture_function,
    )
    generator = fixture_function()
    async for value in generator:
        yield value


class RecordingDeletion:
    def __init__(self) -> None:
        self.calls: list[UUID] = []

    async def delete_account(self, user_id: UUID) -> DataDeletionOutcome:
        self.calls.append(user_id)
        return (
            DataDeletionOutcome.DELETED
            if len(self.calls) == 1
            else DataDeletionOutcome.ALREADY_DELETED
        )


async def test_actual_privacy_screen_prompt_and_cancel(privacy_harness: Harness) -> None:
    dispatcher, bot, session, _, onboarding = privacy_harness
    common = {"onboarding": onboarding, "privacy_retention_days": 30}
    await dispatcher.feed_update(bot, start_update(), **common)
    await dispatcher.feed_update(bot, callback_update("menu:privacy", 2), **common)
    await dispatcher.feed_update(bot, callback_update("privacy:delete_all", 3), **common)
    await dispatcher.feed_update(bot, callback_update("privacy:cancel", 4), **common)
    rendered = [method.text for method in session.methods if isinstance(method, SendMessage)]
    assert any("30" in value and "зашифрована" in value for value in rendered)
    assert texts.DELETE_ALL_PROMPT in rendered
    assert texts.DELETE_ALL_CANCELLED in rendered


async def test_actual_confirmation_is_idempotent_and_clears_fsm(
    privacy_harness: Harness,
) -> None:
    dispatcher, bot, session, users, onboarding = privacy_harness
    deletion = RecordingDeletion()
    common = {
        "onboarding": onboarding,
        "privacy_retention_days": 30,
        "data_deletion": deletion,
    }
    await dispatcher.feed_update(bot, start_update(), **common)
    await dispatcher.feed_update(bot, callback_update("privacy:confirm_all", 2), **common)
    await dispatcher.feed_update(bot, callback_update("privacy:confirm_all", 3), **common)
    rendered = [method.text for method in session.methods if isinstance(method, SendMessage)]
    assert rendered.count(texts.DELETE_ALL_DONE) == 2
    assert len(deletion.calls) == 2 and deletion.calls[0] == deletion.calls[1]
    assert 42 in users.users


@pytest.mark.postgres
async def test_postgres_privacy_handler_tombstones_and_isolates_recreated_account(
    privacy_harness: Harness,
    payment_db: async_sessionmaker[AsyncSession],
) -> None:
    dispatcher, bot, telegram, _, _ = privacy_harness
    sentinel = "PRIVATE-FSM-HANDLER-SENTINEL-9c83"
    cipher = AESGCMSensitiveContentCipher("privacy-handler-postgres-test-key-material")
    async with payment_db() as session:
        users = SqlAlchemyUserRepository(session)
        onboarding = OnboardingService(users, NoOpAnalyticsClient())
        user, _ = await onboarding.start(TelegramIdentity(42, "anna", "Анна", "ru"))
        await onboarding.confirm_age(42)
        await onboarding.accept_consent(42)
        analyses = SqlAlchemyAnalysisRepository(session, cipher, 30)
        draft, _ = await analyses.create_or_resume(user.id)
        draft.intake_step = "waiting_for_goal"
        await analyses.store_private_source(
            draft,
            AnalysisSource(
                messages=[{"text": sentinel}],
                participants={"A": sentinel},
                user_goal=sentinel,
            ),
        )
        completed = Analysis(
            user_id=user.id,
            status="completed",
            intake_step="complete",
            completed_at=datetime.now(UTC),
            report_access="full",
        )
        session.add(completed)
        await session.flush()
        private_content = EncryptedAnalysisContentRepository(session, cipher, 30)
        assert await private_content.store_result(completed.id, {"summary": sentinel})
        await session.commit()
        original_user_id = user.id
        draft_id, completed_id = draft.id, completed.id

        context = dispatcher.fsm.get_context(bot=bot, chat_id=42, user_id=42)
        await context.set_state("private_state")
        await context.set_data({"private": sentinel})
        common = {
            "onboarding": onboarding,
            "privacy_retention_days": 30,
            "data_deletion": DataDeletionService(session, NoOpAnalyticsClient()),
        }
        await dispatcher.feed_update(bot, callback_update("menu:privacy", 20), **common)
        await dispatcher.feed_update(bot, callback_update("privacy:delete_all", 21), **common)
        await dispatcher.feed_update(bot, callback_update("privacy:confirm_all", 22), **common)
        assert await context.get_state() is None
        assert await context.get_data() == {}

        tombstone = await session.get(User, original_user_id)
        assert tombstone is not None and tombstone.privacy_status == "deleted"
        assert tombstone.telegram_user_id is None
        assert await analyses.load_private_source(draft) is None
        report_service = ReportService(analyses, ReportRenderer(), NoOpAnalyticsClient())
        assert (
            await report_service.retrieve(completed_id, original_user_id)
        ).status is ReportStatus.DELETED

        await dispatcher.feed_update(bot, callback_update("privacy:confirm_all", 23), **common)
        await dispatcher.feed_update(bot, start_update(24), **common)
        recreated = await onboarding.current_user(42)
        assert recreated is not None and recreated.id != original_user_id
        assert recreated.privacy_status == "active"
        assert await analyses.get_owned(draft_id, recreated.id) is None
        assert await analyses.get_owned(completed_id, recreated.id) is None
        assert (
            await session.scalar(
                select(func.count())
                .select_from(CreditTransaction)
                .where(CreditTransaction.user_id == recreated.id)
            )
            == 0
        )
        rendered = [method.text for method in telegram.methods if isinstance(method, SendMessage)]
        assert all(sentinel not in value for value in rendered)
