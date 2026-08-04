"""Encrypted durable inbox and lease state machine for Telegram updates."""

import hashlib
import json
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from enum import StrEnum
from typing import cast
from uuid import UUID, uuid4

from sqlalchemy import or_, select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.db.telegram_models import TelegramUpdateInbox
from app.services.sensitive_content import (
    ContentPurpose,
    SensitiveContentCipher,
    SensitiveContentError,
)

AfterLockHook = Callable[[int], Awaitable[None]]


class TelegramAcceptOutcome(StrEnum):
    ACCEPTED = "accepted"
    DUPLICATE = "duplicate"
    PAYLOAD_MISMATCH = "payload_mismatch"


@dataclass(frozen=True)
class TelegramAcceptResult:
    outcome: TelegramAcceptOutcome
    status: str


@dataclass(frozen=True)
class ClaimedTelegramUpdate:
    update_id: int
    claim_id: UUID
    payload: dict[str, object]


class TelegramUpdateInboxService:
    """Persist once, claim with a lease, and erase private payload after terminal state."""

    def __init__(
        self,
        sessions: async_sessionmaker[AsyncSession],
        cipher: SensitiveContentCipher,
        *,
        lease_seconds: int = 300,
        retry_base_seconds: int = 5,
        max_attempts: int = 8,
    ) -> None:
        if lease_seconds <= 0 or retry_base_seconds <= 0 or max_attempts <= 0:
            raise ValueError("Telegram inbox bounds must be positive")
        self._sessions = sessions
        self._cipher = cipher
        self._lease_seconds = lease_seconds
        self._retry_base_seconds = retry_base_seconds
        self._max_attempts = max_attempts

    @staticmethod
    def payload_hash(payload: dict[str, object]) -> str:
        canonical = json.dumps(
            payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")
        ).encode("utf-8")
        return hashlib.sha256(canonical).hexdigest()

    async def accept(
        self,
        update_id: int,
        telegram_user_id: int | None,
        payload: dict[str, object],
    ) -> TelegramAcceptResult:
        digest = self.payload_hash(payload)
        ciphertext = self._cipher.encrypt_json(ContentPurpose.TELEGRAM_UPDATE, payload)
        accepted_at = datetime.now(UTC)
        async with self._sessions.begin() as session:
            inserted = await session.scalar(
                insert(TelegramUpdateInbox)
                .values(
                    update_id=update_id,
                    telegram_user_id=telegram_user_id,
                    payload_ciphertext=ciphertext,
                    payload_hash=digest,
                    status="pending",
                    attempt_count=0,
                    available_at=accepted_at,
                )
                .on_conflict_do_nothing(index_elements=["update_id"])
                .returning(TelegramUpdateInbox.update_id)
            )
            row = await session.scalar(
                select(TelegramUpdateInbox)
                .where(TelegramUpdateInbox.update_id == update_id)
                .with_for_update()
            )
            if row is None:
                raise RuntimeError("Telegram inbox insert was not visible")
            if row.payload_hash != digest:
                self._terminal(row, "duplicate_payload_mismatch")
                return TelegramAcceptResult(TelegramAcceptOutcome.PAYLOAD_MISMATCH, row.status)
            if (
                row.telegram_user_id is None
                and telegram_user_id is not None
                and row.status
                in {
                    "pending",
                    "claimed",
                }
            ):
                row.telegram_user_id = telegram_user_id
            return TelegramAcceptResult(
                TelegramAcceptOutcome.ACCEPTED
                if inserted is not None
                else TelegramAcceptOutcome.DUPLICATE,
                row.status,
            )

    async def claim_one(
        self,
        worker_id: str,
        *,
        now: datetime | None = None,
        after_lock: AfterLockHook | None = None,
    ) -> ClaimedTelegramUpdate | None:
        timestamp = now or datetime.now(UTC)
        async with self._sessions.begin() as session:
            row = await session.scalar(
                select(TelegramUpdateInbox)
                .where(
                    TelegramUpdateInbox.available_at <= timestamp,
                    or_(
                        TelegramUpdateInbox.status == "pending",
                        (TelegramUpdateInbox.status == "claimed")
                        & (TelegramUpdateInbox.lease_until < timestamp),
                    ),
                )
                .order_by(TelegramUpdateInbox.available_at, TelegramUpdateInbox.update_id)
                .with_for_update(skip_locked=True)
                .limit(1)
            )
            if row is None:
                return None
            if after_lock is not None:
                await after_lock(row.update_id)
            if row.payload_ciphertext is None:
                self._terminal(row, "missing_encrypted_payload", timestamp)
                return None
            claim_id = uuid4()
            row.status = "claimed"
            row.claimed_by = worker_id
            row.claim_id = claim_id
            row.claimed_at = timestamp
            row.lease_until = timestamp + timedelta(seconds=self._lease_seconds)
            row.attempt_count += 1
            update_id = row.update_id
            ciphertext = bytes(row.payload_ciphertext)

        try:
            value = self._cipher.decrypt_json(ContentPurpose.TELEGRAM_UPDATE, ciphertext)
        except SensitiveContentError:
            await self.fail_permanent(update_id, claim_id, "encrypted_payload_invalid")
            return None
        if not isinstance(value, dict):
            await self.fail_permanent(update_id, claim_id, "invalid_payload_shape")
            return None
        return ClaimedTelegramUpdate(
            update_id,
            claim_id,
            cast(dict[str, object], value),
        )

    async def complete(self, update_id: int, claim_id: UUID) -> bool:
        async with self._sessions.begin() as session:
            row = await session.get(TelegramUpdateInbox, update_id, with_for_update=True)
            if row is None or row.status != "claimed" or row.claim_id != claim_id:
                return False
            self._terminal(row, None)
            return True

    async def retry(self, update_id: int, claim_id: UUID, code: str) -> bool:
        async with self._sessions.begin() as session:
            row = await session.get(TelegramUpdateInbox, update_id, with_for_update=True)
            if row is None or row.status != "claimed" or row.claim_id != claim_id:
                return False
            if row.attempt_count >= self._max_attempts:
                self._terminal(row, "retry_exhausted")
            else:
                row.status = "pending"
                row.available_at = datetime.now(UTC) + timedelta(
                    seconds=min(
                        self._retry_base_seconds * 2 ** (row.attempt_count - 1),
                        3600,
                    )
                )
                row.last_error_code = code
                self._clear_claim(row)
            return True

    async def fail_permanent(self, update_id: int, claim_id: UUID, code: str) -> bool:
        async with self._sessions.begin() as session:
            row = await session.get(TelegramUpdateInbox, update_id, with_for_update=True)
            if row is None or row.status != "claimed" or row.claim_id != claim_id:
                return False
            self._terminal(row, code)
            return True

    @staticmethod
    def _clear_claim(row: TelegramUpdateInbox) -> None:
        row.claimed_by = None
        row.claim_id = None
        row.claimed_at = None
        row.lease_until = None

    @classmethod
    def _terminal(
        cls,
        row: TelegramUpdateInbox,
        error_code: str | None,
        now: datetime | None = None,
    ) -> None:
        row.status = "failed" if error_code is not None else "completed"
        row.telegram_user_id = None
        row.payload_ciphertext = None
        row.last_error_code = error_code
        row.completed_at = now or datetime.now(UTC)
        cls._clear_claim(row)
