"""Encrypted PostgreSQL FSM storage and distributed aiogram event isolation."""

import hashlib
import json
from collections.abc import AsyncGenerator, Mapping
from contextlib import asynccontextmanager
from typing import Any

from aiogram.fsm.state import State
from aiogram.fsm.storage.base import (
    BaseEventIsolation,
    BaseStorage,
    StateType,
    StorageKey,
)
from sqlalchemy import delete, select, text
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from app.db.fsm_models import TelegramFSMState
from app.services.sensitive_content import ContentPurpose, SensitiveContentCipher


class InvalidFSMDataError(ValueError):
    """Safe error raised when encrypted FSM data is not a JSON object."""


def _identity(key: StorageKey) -> dict[str, int | str]:
    return {
        "bot_id": key.bot_id,
        "chat_id": key.chat_id,
        "user_id": key.user_id,
        "thread_id": key.thread_id or 0,
        "business_connection_id": key.business_connection_id or "",
        "destiny": key.destiny,
    }


def _lock_id(key: StorageKey, *, domain: bytes) -> int:
    payload = json.dumps(
        [
            key.bot_id,
            key.chat_id,
            key.user_id,
            key.thread_id,
            key.business_connection_id,
            key.destiny,
        ],
        ensure_ascii=True,
        separators=(",", ":"),
    ).encode()
    digest = hashlib.blake2b(payload, digest_size=8, person=domain).digest()
    return int.from_bytes(digest, byteorder="big", signed=True)


def _matches(key: StorageKey) -> tuple[object, ...]:
    values = _identity(key)
    return (
        TelegramFSMState.bot_id == values["bot_id"],
        TelegramFSMState.chat_id == values["chat_id"],
        TelegramFSMState.user_id == values["user_id"],
        TelegramFSMState.thread_id == values["thread_id"],
        TelegramFSMState.business_connection_id == values["business_connection_id"],
        TelegramFSMState.destiny == values["destiny"],
    )


class PostgresFSMStorage(BaseStorage):
    """Persist state and encrypted JSON data in the application's PostgreSQL database."""

    def __init__(
        self,
        sessions: async_sessionmaker[AsyncSession],
        cipher: SensitiveContentCipher,
    ) -> None:
        self._sessions = sessions
        self._cipher = cipher

    async def _lock_write(self, session: AsyncSession, key: StorageKey) -> None:
        await session.execute(
            text("SELECT pg_advisory_xact_lock(:lock_id)"),
            {"lock_id": _lock_id(key, domain=b"HSFSMWriteV1")},
        )

    async def _get_row(
        self,
        session: AsyncSession,
        key: StorageKey,
        *,
        for_update: bool = False,
    ) -> TelegramFSMState | None:
        statement = select(TelegramFSMState).where(*_matches(key))
        if for_update:
            statement = statement.with_for_update()
        return await session.scalar(statement)

    def _decode_data(self, row: TelegramFSMState | None) -> dict[str, Any]:
        if row is None or row.data_ciphertext is None:
            return {}
        value = self._cipher.decrypt_json(
            ContentPurpose.TELEGRAM_FSM_DATA,
            bytes(row.data_ciphertext),
        )
        if not isinstance(value, dict) or not all(isinstance(key, str) for key in value):
            raise InvalidFSMDataError("FSM data must be a JSON object with string keys")
        return dict(value)

    def _encode_data(self, data: Mapping[str, Any]) -> bytes | None:
        copied = dict(data)
        if not copied:
            return None
        return self._cipher.encrypt_json(ContentPurpose.TELEGRAM_FSM_DATA, copied)

    async def set_state(self, key: StorageKey, state: StateType = None) -> None:
        normalized = state.state if isinstance(state, State) else state
        async with self._sessions.begin() as session:
            await self._lock_write(session, key)
            row = await self._get_row(session, key, for_update=True)
            if row is None:
                if normalized is None:
                    return
                session.add(TelegramFSMState(**_identity(key), state=normalized))
                return
            row.state = normalized
            if row.state is None and row.data_ciphertext is None:
                await session.delete(row)

    async def get_state(self, key: StorageKey) -> str | None:
        async with self._sessions() as session:
            row = await self._get_row(session, key)
            return None if row is None else row.state

    async def set_data(self, key: StorageKey, data: Mapping[str, Any]) -> None:
        ciphertext = self._encode_data(data)
        async with self._sessions.begin() as session:
            await self._lock_write(session, key)
            row = await self._get_row(session, key, for_update=True)
            if row is None:
                if ciphertext is None:
                    return
                session.add(TelegramFSMState(**_identity(key), data_ciphertext=ciphertext))
                return
            row.data_ciphertext = ciphertext
            if row.state is None and row.data_ciphertext is None:
                await session.delete(row)

    async def get_data(self, key: StorageKey) -> dict[str, Any]:
        async with self._sessions() as session:
            return self._decode_data(await self._get_row(session, key))

    async def update_data(
        self,
        key: StorageKey,
        data: Mapping[str, Any],
    ) -> dict[str, Any]:
        async with self._sessions.begin() as session:
            await self._lock_write(session, key)
            row = await self._get_row(session, key, for_update=True)
            merged = self._decode_data(row)
            merged.update(data)
            ciphertext = self._encode_data(merged)
            if row is None:
                if ciphertext is not None:
                    session.add(TelegramFSMState(**_identity(key), data_ciphertext=ciphertext))
            else:
                row.data_ciphertext = ciphertext
                if row.state is None and row.data_ciphertext is None:
                    await session.delete(row)
            return merged.copy()

    async def close(self) -> None:
        """The application owns the shared SQLAlchemy engine lifecycle."""

    async def delete_user(self, telegram_user_id: int) -> int:
        """Explicit operational cleanup; the database trigger remains the hard boundary."""
        async with self._sessions.begin() as session:
            result = await session.execute(
                delete(TelegramFSMState).where(
                    (TelegramFSMState.user_id == telegram_user_id)
                    | (TelegramFSMState.chat_id == telegram_user_id)
                )
            )
            return result.rowcount


class PostgresEventIsolation(BaseEventIsolation):
    """Serialize aiogram events for one StorageKey across worker processes."""

    def __init__(self, engine: AsyncEngine) -> None:
        self._engine = engine

    @asynccontextmanager
    async def lock(self, key: StorageKey) -> AsyncGenerator[None, None]:
        lock_id = _lock_id(key, domain=b"HSFSMEventV1")
        acquired = False
        async with self._engine.connect() as connection:
            try:
                await connection.execute(
                    text("SELECT pg_advisory_lock(:lock_id)"),
                    {"lock_id": lock_id},
                )
                acquired = True
                yield None
            finally:
                if acquired:
                    await connection.execute(
                        text("SELECT pg_advisory_unlock(:lock_id)"),
                        {"lock_id": lock_id},
                    )

    async def close(self) -> None:
        """The application owns the shared SQLAlchemy engine lifecycle."""
