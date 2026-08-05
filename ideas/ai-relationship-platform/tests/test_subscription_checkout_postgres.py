import asyncio
from datetime import UTC, datetime
from uuid import UUID, uuid4

import pytest
from pydantic import SecretStr
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.config import Settings
from app.db.models import BillingJob, PaymentOrder, User
from app.domain.billing import BillingCatalog
from app.providers.payments.base import PaymentProviderName, UnknownProviderOutcome
from app.providers.payments.subscription_gateway import (
    CreateSubscriptionCheckout,
    HostedSubscriptionCheckout,
    SubscriptionProviderFact,
    SubscriptionStateFact,
)
from app.services.subscription_checkout_service import SubscriptionCheckoutService
from tests.payment_postgres_helpers import payment_db  # noqa: F401

pytestmark = pytest.mark.postgres


class FakeSubscriptionGateway:
    def __init__(self, *, unknown: bool = False) -> None:
        self.unknown = unknown
        self.calls: list[CreateSubscriptionCheckout] = []

    async def create_subscription_checkout(
        self, request: CreateSubscriptionCheckout
    ) -> HostedSubscriptionCheckout:
        self.calls.append(request)
        await asyncio.sleep(0.02)
        if self.unknown:
            raise UnknownProviderOutcome
        return HostedSubscriptionCheckout(
            checkout_id="cs_subscription_one",
            url="https://provider.test/subscription",
            status="open",
            expires_at=datetime.now(UTC),
            live_mode=False,
        )

    async def fetch_subscription_event(
        self, event_type: str, object_id: str
    ) -> SubscriptionProviderFact:
        raise AssertionError((event_type, object_id))

    async def fetch_subscription(self, subscription_id: str) -> SubscriptionProviderFact:
        raise AssertionError(subscription_id)

    async def cancel_subscription(self, subscription_id: str) -> SubscriptionStateFact:
        raise AssertionError(subscription_id)

    async def resume_subscription(self, subscription_id: str) -> SubscriptionStateFact:
        raise AssertionError(subscription_id)


def settings() -> Settings:
    return Settings(
        app_env="test",
        database_url="postgresql+asyncpg://u:p@db/x",
        telegram_bot_token=SecretStr("123456789:AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA"),
        content_encryption_key=SecretStr("test-only-strong-content-key-32-bytes"),
        billing_enabled=True,
        payment_provider="production",
        payment_public_base_url="https://pay.example",
        subscriptions_enabled=True,
        stripe_price_subscription_monthly_eur="price_monthly_eur",
        stripe_amount_subscription_monthly_eur_minor=990,
        product_subscription_monthly_credits=30,
    )


async def create_user(sessions: async_sessionmaker[AsyncSession]) -> UUID:
    async with sessions.begin() as session:
        user = User(telegram_user_id=uuid4().int % 10**12, first_name="Subscription")
        session.add(user)
        await session.flush()
        return user.id


@pytest.mark.asyncio
async def test_concurrent_subscription_checkout_has_one_provider_owner(
    payment_db: async_sessionmaker[AsyncSession],  # noqa: F811
) -> None:
    gateway = FakeSubscriptionGateway()
    configured = settings()
    service = SubscriptionCheckoutService(
        payment_db,
        configured,
        BillingCatalog(configured),
        {PaymentProviderName.STRIPE: gateway},
    )
    user_id = await create_user(payment_db)

    results = await asyncio.gather(
        *(
            service.create_checkout(
                user_id,
                "subscription_monthly",
                "INTERNATIONAL",
                "EUR",
            )
            for _ in range(10)
        )
    )

    assert len(gateway.calls) == 1
    assert len({result.order_id for result in results}) == 1
    existing = await service.create_checkout(
        user_id,
        "subscription_monthly",
        "INTERNATIONAL",
        "EUR",
    )
    assert existing.url == "https://provider.test/subscription"
    async with payment_db() as session:
        order = await session.scalar(select(PaymentOrder))
        assert order is not None
        assert order.mode == "subscription_initial"
        assert order.amount_minor == 990
        assert order.credits == 30
        assert order.commercial_snapshot["consent_version"] == "billing-v1"
        assert await session.scalar(select(func.count()).select_from(PaymentOrder)) == 1


@pytest.mark.asyncio
async def test_unknown_checkout_creates_one_durable_reconciliation_job(
    payment_db: async_sessionmaker[AsyncSession],  # noqa: F811
) -> None:
    gateway = FakeSubscriptionGateway(unknown=True)
    configured = settings()
    service = SubscriptionCheckoutService(
        payment_db,
        configured,
        BillingCatalog(configured),
        {PaymentProviderName.STRIPE: gateway},
    )
    user_id = await create_user(payment_db)

    first = await service.create_checkout(
        user_id,
        "subscription_monthly",
        "INTERNATIONAL",
        "EUR",
    )
    second = await service.create_checkout(
        user_id,
        "subscription_monthly",
        "INTERNATIONAL",
        "EUR",
    )

    assert first.order_id == second.order_id
    assert len(gateway.calls) == 1
    async with payment_db() as session:
        job = await session.scalar(select(BillingJob))
        assert job is not None
        assert job.job_type == "subscription_checkout_reconcile"
        assert await session.scalar(select(func.count()).select_from(BillingJob)) == 1
