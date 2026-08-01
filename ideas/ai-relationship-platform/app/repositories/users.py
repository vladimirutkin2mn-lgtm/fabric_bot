"""User persistence boundary and PostgreSQL implementation."""

from collections.abc import Callable
from typing import Protocol

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import User


class UserRepository(Protocol):
    """Operations required by onboarding, allowing lightweight test doubles."""

    async def get_or_create(
        self,
        telegram_user_id: int,
        username: str | None,
        first_name: str,
        language: str | None,
    ) -> tuple[User, bool]: ...

    async def get_by_telegram_id(self, telegram_user_id: int) -> User | None: ...

    async def save(self, user: User) -> None: ...


class SqlAlchemyUserRepository:
    """Atomic PostgreSQL user repository.

    ``ON CONFLICT`` makes concurrent Telegram updates converge on one row rather
    than relying on a check-then-insert race.
    """

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def get_or_create(
        self,
        telegram_user_id: int,
        username: str | None,
        first_name: str,
        language: str | None,
    ) -> tuple[User, bool]:
        statement = (
            insert(User)
            .values(
                telegram_user_id=telegram_user_id,
                telegram_username=username,
                first_name=first_name,
                telegram_language=language,
            )
            .on_conflict_do_nothing(index_elements=[User.telegram_user_id])
            .returning(User.id)
        )
        inserted_id = (await self._session.execute(statement)).scalar_one_or_none()
        await self._session.commit()
        user = await self.get_by_telegram_id(telegram_user_id)
        if user is None:  # pragma: no cover - protected by the database constraint
            raise RuntimeError("User upsert did not return a persisted row")
        profile = (username, first_name, language)
        stored_profile = (
            user.telegram_username,
            user.first_name,
            user.telegram_language,
        )
        if stored_profile != profile:
            user.telegram_username = username
            user.first_name = first_name
            user.telegram_language = language
            await self.save(user)
        return user, inserted_id is not None

    async def get_by_telegram_id(self, telegram_user_id: int) -> User | None:
        result = await self._session.execute(
            select(User).where(User.telegram_user_id == telegram_user_id)
        )
        return result.scalar_one_or_none()

    async def save(self, user: User) -> None:
        self._session.add(user)
        await self._session.commit()
        await self._session.refresh(user)


UserRepositoryFactory = Callable[[AsyncSession], UserRepository]
