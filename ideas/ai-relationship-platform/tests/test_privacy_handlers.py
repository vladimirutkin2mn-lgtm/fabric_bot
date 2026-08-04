"""Actual aiogram privacy callbacks with MemoryStorage and a recording session."""

from collections.abc import AsyncGenerator, Callable
from typing import cast
from uuid import UUID

import pytest
from aiogram.methods import SendMessage

from app.bot import texts
from app.services.data_deletion import DataDeletionOutcome
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
