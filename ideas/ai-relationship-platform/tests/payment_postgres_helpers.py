"""Shared real-PostgreSQL fixtures for M5B.2 payment tests."""

import os
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.db.base import Base
from app.db.models import BillingJob, PaymentOrder, ProviderWebhookEvent, User
from app.providers.payments.gateway import AuthoritativePayment, CreateCheckout, HostedCheckout


@pytest.fixture
async def payment_db() -> AsyncIterator[async_sessionmaker[AsyncSession]]:
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


class FakeGateway:
    def __init__(self, payment: AuthoritativePayment | None = None) -> None:
        self.payment = payment
        self.create_keys: list[str] = []
        self.fetches = 0

    async def create_checkout(self, request: CreateCheckout) -> HostedCheckout:
        self.create_keys.append(request.idempotency_key)
        return HostedCheckout("checkout-one", "https://provider.test/checkout", "pending")

    async def fetch_payment(self, checkout_id: str) -> AuthoritativePayment:
        self.fetches += 1
        assert self.payment is not None
        return self.payment


async def create_order(
    sessions: async_sessionmaker[AsyncSession],
    *,
    provider: str = "stripe",
    status: str = "pending",
    checkout_id: str | None = None,
    payment_id: str | None = None,
) -> tuple[UUID, UUID]:
    async with sessions.begin() as session:
        user = User(telegram_user_id=uuid4().int % 10**12, first_name="Billing")
        session.add(user)
        await session.flush()
        order = PaymentOrder(
            user_id=user.id,
            provider=provider,
            product_code="analysis_single",
            status=status,
            credits=1,
            amount_minor=500,
            currency="EUR",
            market="INTERNATIONAL",
            mode="one_time",
            product_version=1,
            provider_checkout_id=checkout_id or f"checkout-{uuid4()}",
            provider_payment_id=payment_id,
            idempotency_key=f"checkout:create:{uuid4()}:v1",
            commercial_snapshot={
                "product_code": "analysis_single",
                "product_version": 1,
                "credits": 1,
                "amount_minor": 500,
                "currency": "EUR",
                "provider": provider,
                "market": "INTERNATIONAL",
                "price_reference": "price_test",
                "billing_period": None,
            },
        )
        session.add(order)
        await session.flush()
        return user.id, order.id


async def create_claimed_job(
    sessions: async_sessionmaker[AsyncSession],
    order_id: UUID,
    *,
    webhook: bool = False,
    checkout_id: str | None = None,
) -> tuple[UUID, UUID, UUID | None]:
    async with sessions.begin() as session:
        event_id = None
        object_id = str(order_id)
        job_type = "payment_reconciliation"
        object_type = "payment_order"
        if webhook:
            event = ProviderWebhookEvent(
                provider="stripe",
                provider_event_id=f"evt-{uuid4()}",
                event_type="checkout.session.completed",
                provider_object_id=checkout_id or "checkout",
                payload_hash="a" * 64,
                status="processing",
            )
            session.add(event)
            await session.flush()
            event_id, object_id = event.id, str(event.id)
            job_type, object_type = "webhook_processing", "webhook_event"
        claim_id = uuid4()
        job = BillingJob(
            job_type=job_type,
            provider="stripe",
            object_type=object_type,
            object_id=object_id,
            idempotency_key=f"job:{uuid4()}",
            status="claimed",
            claimed_by="test",
            claim_id=claim_id,
            claimed_at=datetime.now(UTC),
            lease_until=datetime.now(UTC) + timedelta(minutes=5),
            attempt_count=1,
        )
        session.add(job)
        await session.flush()
        return job.id, claim_id, event_id


def paid(order_id: UUID, checkout_id: str, payment_id: str = "payment-one") -> AuthoritativePayment:
    return AuthoritativePayment(
        checkout_id=checkout_id,
        payment_id=payment_id,
        status="succeeded",
        amount_minor=500,
        currency="EUR",
        order_id=str(order_id),
        paid=True,
        live_mode=False,
        provider_status="paid",
    )
