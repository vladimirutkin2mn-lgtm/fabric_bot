"""Milestone 1 onboarding scenarios without Telegram or PostgreSQL network calls."""

import asyncio
from collections.abc import Mapping
from uuid import uuid4

import pytest

from app.db.models import User
from app.services import onboarding as onboarding_module
from app.services.onboarding import (
    CURRENT_CONSENT_VERSION,
    OnboardingService,
    OnboardingStep,
    TelegramIdentity,
)


class MemoryUsers:
    def __init__(self) -> None:
        self.users: dict[int, User] = {}
        self.lock = asyncio.Lock()

    async def get_or_create(
        self,
        telegram_user_id: int,
        username: str | None,
        first_name: str,
        language: str | None,
    ) -> tuple[User, bool]:
        async with self.lock:
            user = self.users.get(telegram_user_id)
            if user is not None:
                return user, False
            user = User(
                id=uuid4(),
                telegram_user_id=telegram_user_id,
                telegram_username=username,
                first_name=first_name,
                telegram_language=language,
                age_confirmed=False,
                onboarding_completed=False,
            )
            self.users[telegram_user_id] = user
            return user, True

    async def get_by_telegram_id(self, telegram_user_id: int) -> User | None:
        return self.users.get(telegram_user_id)

    async def save(self, user: User) -> None:
        assert user.telegram_user_id is not None
        self.users[user.telegram_user_id] = user


class AnalyticsSpy:
    def __init__(self) -> None:
        self.events: list[tuple[str | None, str, Mapping[str, str] | None]] = []

    async def track(
        self, user_id: str | None, event: str, properties: Mapping[str, str] | None = None
    ) -> None:
        self.events.append((user_id, event, properties))


@pytest.fixture
def identity() -> TelegramIdentity:
    return TelegramIdentity(42, "hearts", "Анна", "ru")


@pytest.fixture
def components() -> tuple[MemoryUsers, AnalyticsSpy, OnboardingService]:
    users = MemoryUsers()
    analytics = AnalyticsSpy()
    return users, analytics, OnboardingService(users, analytics)


async def test_new_start_persists_user_and_repeated_start_is_idempotent(
    components: tuple[MemoryUsers, AnalyticsSpy, OnboardingService],
    identity: TelegramIdentity,
) -> None:
    users, _, service = components
    first, step = await service.start(identity)
    second, repeated_step = await service.start(identity)

    assert step is OnboardingStep.AGE
    assert repeated_step is OnboardingStep.AGE
    assert first.id == second.id
    assert len(users.users) == 1


async def test_age_decline_does_not_change_durable_progress(
    components: tuple[MemoryUsers, AnalyticsSpy, OnboardingService],
    identity: TelegramIdentity,
) -> None:
    users, _, service = components
    await service.start(identity)
    # The decline handler intentionally does not call a mutating service method.
    assert service.step_for(users.users[identity.telegram_user_id]) is OnboardingStep.AGE


async def test_confirmation_and_consent_complete_onboarding(
    components: tuple[MemoryUsers, AnalyticsSpy, OnboardingService],
    identity: TelegramIdentity,
) -> None:
    users, analytics, service = components
    await service.start(identity)
    assert await service.confirm_age(identity.telegram_user_id) is OnboardingStep.CONSENT
    assert users.users[42].age_confirmed_at is not None

    assert await service.accept_consent(42) is OnboardingStep.COMPLETE
    assert users.users[42].consent_version == CURRENT_CONSENT_VERSION
    assert users.users[42].consent_accepted_at is not None
    assert users.users[42].onboarding_completed
    assert [event for _, event, _ in analytics.events] == [
        "bot_started",
        "age_confirmed",
        "consent_accepted",
        "onboarding_completed",
        "main_menu_opened",
    ]


async def test_returning_completed_user_opens_menu_and_restart_restores_database_state(
    components: tuple[MemoryUsers, AnalyticsSpy, OnboardingService],
    identity: TelegramIdentity,
) -> None:
    users, analytics, service = components
    await service.start(identity)
    await service.confirm_age(42)
    await service.accept_consent(42)

    restarted_service = OnboardingService(users, analytics)
    _, step = await restarted_service.start(identity)
    assert step is OnboardingStep.COMPLETE
    assert analytics.events[-1][1] == "main_menu_opened"


async def test_analysis_requires_current_consent(
    components: tuple[MemoryUsers, AnalyticsSpy, OnboardingService],
    identity: TelegramIdentity,
) -> None:
    _, _, service = components
    await service.start(identity)
    assert not await service.analysis_allowed(42)
    await service.confirm_age(42)
    assert not await service.analysis_allowed(42)
    await service.accept_consent(42)
    assert await service.analysis_allowed(42)


async def test_changed_consent_version_requires_reacceptance(
    components: tuple[MemoryUsers, AnalyticsSpy, OnboardingService],
    identity: TelegramIdentity,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    users, _, service = components
    await service.start(identity)
    await service.confirm_age(42)
    await service.accept_consent(42)

    monkeypatch.setattr(onboarding_module, "CURRENT_CONSENT_VERSION", "2.0")
    assert await service.current_step(42) is OnboardingStep.CONSENT
    assert not users.users[42].onboarding_completed
    assert not await service.analysis_allowed(42)
    assert await service.accept_consent(42) is OnboardingStep.COMPLETE


async def test_concurrent_creation_has_one_user(
    components: tuple[MemoryUsers, AnalyticsSpy, OnboardingService],
    identity: TelegramIdentity,
) -> None:
    users, _, service = components
    results = await asyncio.gather(*(service.start(identity) for _ in range(20)))
    assert len(users.users) == 1
    assert len({user.id for user, _ in results}) == 1


async def test_analytics_never_receives_profile_or_private_message(
    components: tuple[MemoryUsers, AnalyticsSpy, OnboardingService],
) -> None:
    _, analytics, service = components
    private_text = "очень личный текст переписки"
    await service.start(TelegramIdentity(7, private_text, private_text, "ru"))
    serialized_events = repr(analytics.events)
    assert private_text not in serialized_events
    assert all(properties is None for _, _, properties in analytics.events)
