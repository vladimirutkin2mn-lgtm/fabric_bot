"""Real-PostgreSQL exactly-once refund reconciliation regressions."""

import os
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

import pytest
from pydantic import SecretStr
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.config import Settings
from app.db.base import Base
from app.db.models import (
    BillingJob,
    CreditReservation,
    CreditTransaction,
    PaymentOrder,
    RefundRequest,
    User,
)
from app.providers.payments.refund_gateway import (
    AuthoritativeRefund,
    CreateRefund,
    RefundCapabilities,
)
from app.services.billing_job_worker import BillingJobWorker
from app.services.payment_completion_service import PaymentCompletionService
from app.services.refund_reconciliation_service import RefundReconciliationService
from app.services.refund_service import RefundRequestOutcome, RefundService

pytestmark = pytest.mark.postgres


class FakeRefundGateway:
    refund_capabilities = RefundCapabilities(partial_refunds=True)

    def __init__(self, status: str = "succeeded") -> None:
        self.status = status
        self.create_calls = 0
        self.fetch_calls = 0
        self.refund_id = "refund-provider-1"
        self.payment_id = "payment-provider-1"
        self.amount_minor = 1_000
        self.currency = "EUR"

    def fact(self) -> AuthoritativeRefund:
        return AuthoritativeRefund(
            provider="stripe",
            provider_refund_id=self.refund_id,
            provider_payment_id=self.payment_id,
            status=self.status,
            amount_minor=self.amount_minor,
            currency=self.currency,
            provider_status=self.status,
            failure_code="declined" if self.status == "failed" else None,
            live_mode=False,
        )

    async def create_refund(self, request: CreateRefund) -> AuthoritativeRefund:
        self.create_calls += 1
        assert request.provider_payment_id == self.payment_id
        assert request.amount_minor == self.amount_minor
        return self.fact()

    async def fetch_refund(self, refund_id: str) -> AuthoritativeRefund:
        self.fetch_calls += 1
        assert refund_id == self.refund_id
        return self.fact()


@pytest.fixture
async def refund_db() -> AsyncIterator[async_sessionmaker[AsyncSession]]:
    url = os.getenv("TEST_DATABASE_URL")
    if not url:
        pytest.skip("TEST_DATABASE_URL is required")
    engine = create_async_engine(url)
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.drop_all)
        await connection.run_sync(Base.metadata.create_all)
    yield async_sessionmaker(engine, expire_on_commit=False)
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.drop_all)
    await engine.dispose()


def settings() -> Settings:
    return Settings(
        app_env="test",
        database_url="postgresql+asyncpg://u:p@db/x",
        telegram_bot_token=SecretStr("123456789:AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA"),
        content_encryption_key=SecretStr("refund-reconciliation-test-key"),
        payment_provider="stripe",
        billing_enabled=True,
        refunds_enabled=True,
        stripe_enabled=True,
    )


async def requested_refund(
    sessions: async_sessionmaker[AsyncSession], gateway: FakeRefundGateway
) -> tuple[UUID, UUID, UUID]:
    async with sessions.begin() as session:
        user = User(telegram_user_id=uuid4().int % 10**12, first_name="Refund")
        session.add(user)
        await session.flush()
        order = PaymentOrder(
            user_id=user.id,
            provider="stripe",
            product_code="analysis_pack_5",
            status="completed",
            credits=5,
            amount_minor=1_000,
            currency="EUR",
            provider_payment_id=gateway.payment_id,
            provider_status="succeeded",
            provider_live_mode=False,
            completed_at=datetime.now(UTC),
            commercial_snapshot={},
        )
        session.add(order)
        await session.flush()
        session.add(
            CreditTransaction(
                user_id=user.id,
                type="purchase",
                amount=5,
                idempotency_key=f"purchase:{order.id}",
                payment_order_id=order.id,
                product_code=order.product_code,
                external_payment_id=order.provider_payment_id,
                external_payment_provider="stripe",
            )
        )
        user_id, order_id = user.id, order.id
    result = await RefundService(
        sessions,
        settings(),
        {"stripe": gateway},
    ).request_refund(user_id, order_id, 5)
    assert result.outcome is RefundRequestOutcome.CREATED
    assert result.refund is not None
    return user_id, order_id, result.refund.id


def worker(
    sessions: async_sessionmaker[AsyncSession], gateway: FakeRefundGateway
) -> BillingJobWorker:
    return BillingJobWorker(
        sessions,
        {},
        PaymentCompletionService(sessions),
        lease_seconds=60,
        retry_base_seconds=1,
        max_attempts=3,
        refund_gateways={"stripe": gateway},
        refund_processor=RefundReconciliationService(sessions, pending_retry_seconds=1),
    )


async def ledger_refund_count(sessions: async_sessionmaker[AsyncSession]) -> int:
    async with sessions() as session:
        return int(
            await session.scalar(
                select(func.count())
                .select_from(CreditTransaction)
                .where(CreditTransaction.type == "purchase_refund")
            )
            or 0
        )


async def test_success_consumes_reservation_and_posts_one_negative_ledger_entry(
    refund_db: async_sessionmaker[AsyncSession],
) -> None:
    gateway = FakeRefundGateway("succeeded")
    _, order_id, refund_id = await requested_refund(refund_db, gateway)
    jobs = worker(refund_db, gateway)

    assert await jobs.run_once("worker-1")
    assert not await jobs.run_once("worker-1")

    async with refund_db() as session:
        refund = await session.get(RefundRequest, refund_id)
        reservation = await session.scalar(
            select(CreditReservation).where(CreditReservation.refund_request_id == refund_id)
        )
        ledger = await session.scalar(
            select(CreditTransaction).where(CreditTransaction.refund_request_id == refund_id)
        )
        purchase = await session.scalar(
            select(CreditTransaction).where(
                CreditTransaction.payment_order_id == order_id,
                CreditTransaction.type == "purchase",
            )
        )
        assert refund is not None and refund.status == "succeeded"
        assert refund.provider_refund_id == gateway.refund_id
        assert reservation is not None and reservation.status == "consumed"
        assert ledger is not None and ledger.amount == -5
        assert purchase is not None and ledger.original_purchase_transaction_id == purchase.id
    assert await ledger_refund_count(refund_db) == 1
    assert gateway.create_calls == 1


async def test_authoritative_failure_releases_credits_without_ledger_entry(
    refund_db: async_sessionmaker[AsyncSession],
) -> None:
    gateway = FakeRefundGateway("failed")
    user_id, _, refund_id = await requested_refund(refund_db, gateway)

    assert await worker(refund_db, gateway).run_once("worker-1")

    async with refund_db() as session:
        refund = await session.get(RefundRequest, refund_id)
        reservation = await session.scalar(
            select(CreditReservation).where(CreditReservation.refund_request_id == refund_id)
        )
        balance = int(
            await session.scalar(
                select(func.coalesce(func.sum(CreditTransaction.amount), 0)).where(
                    CreditTransaction.user_id == user_id
                )
            )
            or 0
        )
        assert refund is not None and refund.status == "failed"
        assert refund.failure_code == "declined"
        assert reservation is not None and reservation.status == "released"
        assert balance == 5
    assert await ledger_refund_count(refund_db) == 0


async def test_pending_refund_reuses_provider_identity_then_completes(
    refund_db: async_sessionmaker[AsyncSession],
) -> None:
    gateway = FakeRefundGateway("pending")
    _, _, refund_id = await requested_refund(refund_db, gateway)
    jobs = worker(refund_db, gateway)

    assert await jobs.run_once("worker-1")
    async with refund_db.begin() as session:
        refund = await session.get(RefundRequest, refund_id)
        job = await session.scalar(select(BillingJob).where(BillingJob.object_id == str(refund_id)))
        assert refund is not None and refund.status == "provider_pending"
        assert refund.provider_refund_id == gateway.refund_id
        assert job is not None and job.status == "pending"
        job.available_at = datetime.now(UTC) - timedelta(seconds=1)
    gateway.status = "succeeded"

    assert await jobs.run_once("worker-2")

    async with refund_db() as session:
        refund = await session.get(RefundRequest, refund_id)
        assert refund is not None and refund.status == "succeeded"
    assert gateway.create_calls == 1
    assert gateway.fetch_calls == 1
    assert await ledger_refund_count(refund_db) == 1


async def test_provider_amount_mismatch_keeps_reservation_for_manual_review(
    refund_db: async_sessionmaker[AsyncSession],
) -> None:
    gateway = FakeRefundGateway("succeeded")
    _, _, refund_id = await requested_refund(refund_db, gateway)
    gateway.amount_minor = 999

    assert await worker(refund_db, gateway).run_once("worker-1")

    async with refund_db() as session:
        refund = await session.get(RefundRequest, refund_id)
        reservation = await session.scalar(
            select(CreditReservation).where(CreditReservation.refund_request_id == refund_id)
        )
        job = await session.scalar(select(BillingJob).where(BillingJob.object_id == str(refund_id)))
        assert refund is not None and refund.status == "manual_review"
        assert refund.failure_code == "refund_amount_mismatch"
        assert reservation is not None and reservation.status == "active"
        assert job is not None and job.status == "manual_review"
    assert await ledger_refund_count(refund_db) == 0
