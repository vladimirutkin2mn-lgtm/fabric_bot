"""Durable onboarding rules independent of Telegram handlers."""

from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum

from app.db.models import User
from app.providers.analytics import AnalyticsClient
from app.repositories.users import UserRepository

CURRENT_CONSENT_VERSION = "1.0"


class OnboardingStep(StrEnum):
    AGE = "age"
    CONSENT = "consent"
    COMPLETE = "complete"


@dataclass(frozen=True)
class TelegramIdentity:
    telegram_user_id: int
    username: str | None
    first_name: str
    language: str | None


class OnboardingService:
    """Use PostgreSQL progress as the source of truth across restarts."""

    def __init__(self, users: UserRepository, analytics: AnalyticsClient) -> None:
        self._users = users
        self._analytics = analytics

    @staticmethod
    def step_for(user: User) -> OnboardingStep:
        if not user.age_confirmed:
            return OnboardingStep.AGE
        if user.consent_version != CURRENT_CONSENT_VERSION:
            return OnboardingStep.CONSENT
        return OnboardingStep.COMPLETE

    async def start(self, identity: TelegramIdentity) -> tuple[User, OnboardingStep]:
        user, _ = await self._users.get_or_create(
            identity.telegram_user_id,
            identity.username,
            identity.first_name,
            identity.language,
        )
        await self._analytics.track(str(user.id), "bot_started")
        step = await self._synchronize_completion(user)
        if step is OnboardingStep.COMPLETE:
            await self._analytics.track(str(user.id), "main_menu_opened")
        return user, step

    async def confirm_age(self, telegram_user_id: int) -> OnboardingStep:
        user = await self._required_user(telegram_user_id)
        if not user.age_confirmed:
            user.age_confirmed = True
            user.age_confirmed_at = datetime.now(UTC)
            await self._users.save(user)
            await self._analytics.track(str(user.id), "age_confirmed")
        return self.step_for(user)

    async def accept_consent(self, telegram_user_id: int) -> OnboardingStep:
        user = await self._required_user(telegram_user_id)
        if not user.age_confirmed:
            return OnboardingStep.AGE
        was_complete = self.step_for(user) is OnboardingStep.COMPLETE
        if user.consent_version != CURRENT_CONSENT_VERSION:
            user.consent_version = CURRENT_CONSENT_VERSION
            user.consent_accepted_at = datetime.now(UTC)
            user.onboarding_completed = True
            await self._users.save(user)
            await self._analytics.track(str(user.id), "consent_accepted")
            if not was_complete:
                await self._analytics.track(str(user.id), "onboarding_completed")
        await self._analytics.track(str(user.id), "main_menu_opened")
        return OnboardingStep.COMPLETE

    async def analysis_allowed(self, telegram_user_id: int) -> bool:
        user = await self._users.get_by_telegram_id(telegram_user_id)
        return (
            user is not None and await self._synchronize_completion(user) is OnboardingStep.COMPLETE
        )

    async def current_step(self, telegram_user_id: int) -> OnboardingStep:
        user = await self._users.get_by_telegram_id(telegram_user_id)
        return await self._synchronize_completion(user) if user is not None else OnboardingStep.AGE

    async def current_user(self, telegram_user_id: int) -> User | None:
        return await self._users.get_by_telegram_id(telegram_user_id)

    async def _synchronize_completion(self, user: User) -> OnboardingStep:
        """Keep the persisted convenience flag aligned with current consent rules."""
        step = self.step_for(user)
        completed = step is OnboardingStep.COMPLETE
        if user.onboarding_completed != completed:
            user.onboarding_completed = completed
            await self._users.save(user)
        return step

    async def _required_user(self, telegram_user_id: int) -> User:
        user = await self._users.get_by_telegram_id(telegram_user_id)
        if user is None:
            raise LookupError("Onboarding user does not exist")
        return user
