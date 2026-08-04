"""Durable encrypted Telegram inbox tests on real PostgreSQL."""

import asyncio
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.db.telegram_models import TelegramUpdateInbox
from app.services.sensitive_content import AESGCMSensitiveContentCipher
from app.services.telegram_update_inbox import (
    ClaimedTelegramUpdate,
    TelegramAcceptOutcome,
    TelegramUpdateInboxService,
)

pytestmark = pytest.mark.postgres


def payload(update_id: int, text: str = "private conversation text") -> dict[str, object]:
    return {
        "update_id": update_id,
        "message": {
            "message_id": 1,
            "date": 1_700_000_000,
            "chat": {"id": 42, "type": "private"},
            "from": {"id": 42, "is_bot": False, "first_name": "Test"},
            "text": text,
        },
    }


def service(
    sessions: async_sessionmaker[AsyncSession],
    *,
    lease_seconds: int = 120,
    max_attempts: int = 8,
) -> TelegramUpdateInboxService:
    return TelegramUpdateInboxService(
        sessions,
        AESGCMSensitiveContentCipher(b"telegram-inbox-test-key"),
        lease_seconds=lease_seconds,
        retry_base_seconds=1,
        max_attempts=max_attempts,
    )


async def test_accept_is_idempotent_encrypted_and_completion_erases_payload(
    payment_db: async_sessionmaker[AsyncSession],
) -> None:
    inbox = service(payment_db)
    first = await inbox.accept(1001, 42, payload(1001))
    duplicate = await inbox.accept(1001, 42, payload(1001))
    assert first.outcome is TelegramAcceptOutcome.ACCEPTED
    assert duplicate.outcome is TelegramAcceptOutcome.DUPLICATE

    async with payment_db() as session:
        row = await session.get(TelegramUpdateInbox, 1001)
        assert row is not None and row.status == "pending"
        assert row.payload_ciphertext is not None
        assert b"private conversation text" not in row.payload_ciphertext

    claim = await inbox.claim_one("worker-one")
    assert claim is not None and claim.update_id == 1001
    assert claim.payload == payload(1001)
    assert await inbox.complete(claim.update_id, claim.claim_id)

    async with payment_db() as session:
        row = await session.get(TelegramUpdateInbox, 1001)
        assert row is not None and row.status == "completed"
        assert row.payload_ciphertext is None
        assert row.completed_at is not None


async def test_duplicate_update_id_with_different_payload_fails_closed(
    payment_db: async_sessionmaker[AsyncSession],
) -> None:
    inbox = service(payment_db)
    await inbox.accept(1002, 42, payload(1002, "first"))
    mismatch = await inbox.accept(1002, 42, payload(1002, "different"))
    assert mismatch.outcome is TelegramAcceptOutcome.PAYLOAD_MISMATCH

    async with payment_db() as session:
        row = await session.get(TelegramUpdateInbox, 1002)
        assert row is not None and row.status == "failed"
        assert row.payload_ciphertext is None
        assert row.last_error_code == "duplicate_payload_mismatch"


async def test_two_workers_skip_locked_claim_different_updates(
    payment_db: async_sessionmaker[AsyncSession],
) -> None:
    inbox = service(payment_db)
    await inbox.accept(1010, 42, payload(1010))
    await inbox.accept(1011, 42, payload(1011))
    first_locked = asyncio.Event()
    second_locked = asyncio.Event()

    async def hold_first(_: int) -> None:
        first_locked.set()
        await asyncio.wait_for(second_locked.wait(), timeout=5)

    async def release_second(_: int) -> None:
        second_locked.set()

    async def first_claim() -> ClaimedTelegramUpdate | None:
        return await inbox.claim_one("worker-one", after_lock=hold_first)

    async def second_claim() -> ClaimedTelegramUpdate | None:
        await asyncio.wait_for(first_locked.wait(), timeout=5)
        return await inbox.claim_one("worker-two", after_lock=release_second)

    first, second = await asyncio.gather(first_claim(), second_claim())
    assert first is not None and second is not None
    assert {first.update_id, second.update_id} == {1010, 1011}


async def test_expired_lease_is_reclaimed_and_stale_claim_cannot_complete(
    payment_db: async_sessionmaker[AsyncSession],
) -> None:
    inbox = service(payment_db, lease_seconds=30)
    await inbox.accept(1020, 42, payload(1020))
    now = datetime.now(UTC)
    first = await inbox.claim_one("worker-one", now=now)
    assert first is not None
    second = await inbox.claim_one("worker-two", now=now + timedelta(seconds=31))
    assert second is not None and second.update_id == first.update_id
    assert second.claim_id != first.claim_id
    assert not await inbox.complete(first.update_id, first.claim_id)
    assert await inbox.complete(second.update_id, second.claim_id)


async def test_retry_exhaustion_erases_private_payload(
    payment_db: async_sessionmaker[AsyncSession],
) -> None:
    inbox = service(payment_db, max_attempts=1)
    await inbox.accept(1030, 42, payload(1030))
    claim = await inbox.claim_one("worker")
    assert claim is not None
    assert await inbox.retry(claim.update_id, claim.claim_id, "handler_error")

    async with payment_db() as session:
        row = await session.get(TelegramUpdateInbox, 1030)
        assert row is not None and row.status == "failed"
        assert row.payload_ciphertext is None
        assert row.last_error_code == "retry_exhausted"
