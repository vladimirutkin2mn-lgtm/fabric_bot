"""Real-PostgreSQL refund request and reservation regressions."""

import asyncio
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
from app.providers.payments.refund_gateway import RefundCapabilities
from app.services.refund_service import RefundRequestOutcome, RefundService

pytestmark = pytest.mark.postgres


class CapabilityGateway:
    def __init__(self, partial: bool = True) -> None:
        self.refund_capabilities = RefundCapabilities(partial_refunds=partial)


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
        content_encryption_key=SecretStr("refund-test-key"),
        payment_provider="stripe",
        billing_enabled=True,
        refunds_enabled=True,
        stripe_enabled=True,
        billing_refund_window_days=14,
    )


async def purchase(
    sessions: async_sessionmaker[AsyncSession],
    *,
    provider: str = "stripe",
    credits: int = 5,
    amount_minor: int = 1_000,
    age_days: int = 0,
) -> tuple[UUID, UUID]:
    async with sessions.begin() as session:
        user = User(telegram_user_id=uuid4().int % 10**12, first_name="Refund")
        session.add(user)
        await session.flush()
        completed_at = datetime.now(UTC) - timedelta(days=age_days)
        order = PaymentOrder(
            user_id=user.id,
            provider=provider,
            product_code="analysis_pack_5",
            status="completed",
            credits=credits,
            amount_minor=amount_minor,
            currency="EUR" if provider == "stripe" else "RUB",
            provider_payment_id=f"payment-{uuid4()}",
            provider_status="succeeded",
            provider_live_mode=False,
            completed_at=completed_at,
            commercial_snapshot={},
        )
        session.add(order)
        await session.flush()
        session.add(
            CreditTransaction(
                user_id=user.id,
                type="purchase",
                amount=credits,
                idempotency_key=f"purchase:{order.id}",
                payment_order_id=order.id,
                product_code=order.product_code,
                external_payment_id=order.provider_payment_id,
                external_payment_provider=provider,
            )
        )
        return user.id, order.id


async def counts(
    sessions: async_sessionmaker[AsyncSession],
) -> tuple[int, int, int]:
    async with sessions() as session:
        return (
            int(await session.scalar(select(func.count()).select_from(RefundRequest)) or 0),
            int(await session.scalar(select(func.count()).select_from(CreditReservation)) or 0),
            int(await session.scalar(select(func.count()).select_from(BillingJob)) or 0),
        )


async def test_concurrent_refund_requests_create_one_reservation_and_job(
    refund_db: async_sessionmaker[AsyncSession],
) -> None:
    user_id, order_id = await purchase(refund_db)
    service = RefundService(refund_db, settings(), {"stripe": CapabilityGateway()})  # type: ignore[dict-item]

    results = await asyncio.gather(
        *(service.request_refund(user_id, order_id, 5) for _ in range(10))
    )

    assert sum(result.outcome is RefundRequestOutcome.CREATED for result in results) == 1
    assert sum(result.outcome is RefundRequestOutcome.ALREADY_PENDING for result in results) == 9
    assert await counts(refund_db) == (1, 1, 1)


async def test_partial_refund_uses_integer_minor_units_and_residual_on_last_refund(
    refund_db: async_sessionmaker[AsyncSession],
) -> None:
    user_id, order_id = await purchase(refund_db, credits=3, amount_minor=1_000)
    service = RefundService(refund_db, settings(), {"stripe": CapabilityGateway()})  # type: ignore[dict-item]

    first = await service.request_refund(user_id, order_id, 1)
    assert first.outcome is RefundRequestOutcome.CREATED
    assert first.refund is not None and first.refund.amount_minor == 333

    async with refund_db.begin() as session:
        row = await session.scalar(select(RefundRequest).where(RefundRequest.id == first.refund.id))
        reservation = await session.scalar(
            select(CreditReservation).where(CreditReservation.refund_request_id == first.refund.id)
        )
        assert row is not None and reservation is not None
        row.status = "succeeded"
        reservation.status = "consumed"
        session.add(
            CreditTransaction(
                user_id=user_id,
                type="purchase_refund",
                amount=-1,
                idempotency_key=f"purchase_refund:{row.id}",
                payment_order_id=order_id,
                product_code="analysis_pack_5",
                external_payment_id=f"refund-{row.id}",
                external_payment_provider="stripe",
                original_purchase_transaction_id=(
                    await session.scalar(
                        select(CreditTransaction.id).where(
                            CreditTransaction.payment_order_id == order_id,
                            CreditTransaction.type == "purchase",
                        )
                    )
                ),
                refund_request_id=row.id,
            )
        )

    second = await service.request_refund(user_id, order_id, 2)
    assert second.outcome is RefundRequestOutcome.CREATED
    assert second.refund is not None and second.refund.amount_minor == 667


async def test_used_credits_and_expired_purchase_are_not_eligible(
    refund_db: async_sessionmaker[AsyncSession],
) -> None:
    user_id, order_id = await purchase(refund_db, age_days=15)
    service = RefundService(refund_db, settings(), {"stripe": CapabilityGateway()})  # type: ignore[dict-item]

    result = await service.request_refund(user_id, order_id, 5)

    assert result.outcome is RefundRequestOutcome.NOT_ELIGIBLE
    assert await service.eligible_purchases(user_id) == ()


async def test_provider_without_partial_support_rejects_partial_request(
    refund_db: async_sessionmaker[AsyncSession],
) -> None:
    user_id, order_id = await purchase(refund_db, provider="yookassa")
    service = RefundService(
        refund_db,
        settings(),
        {"yookassa": CapabilityGateway(partial=False)},  # type: ignore[dict-item]
    )

    result = await service.request_refund(user_id, order_id, 1)

    assert result.outcome is RefundRequestOutcome.PARTIAL_UNSUPPORTED
