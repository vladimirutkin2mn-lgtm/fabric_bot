"""PostgreSQL payment checkout ownership and completion regressions."""

import asyncio
import os
from collections.abc import AsyncIterator, Mapping
from uuid import uuid4

import pytest
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.config import Settings
from app.db.base import Base
from app.db.models import CreditTransaction, PaymentOrder, User
from app.domain.products import ProductCatalog
from app.providers.payments.base import Checkout, CheckoutRequest, PaymentEvent
from app.services.payment_service import CheckoutOutcome, PaymentCompletionOutcome, PaymentService

pytestmark = pytest.mark.postgres


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
    await engine.dispose()


class Analytics:
    def __init__(self) -> None:
        self.events: list[str] = []

    async def track(
        self, user_id: str | None, event: str, properties: Mapping[str, str] | None = None
    ) -> None:
        self.events.append(event)


class BlockingProvider:
    def __init__(self) -> None:
        self.calls = 0
        self.started = asyncio.Event()
        self.release = asyncio.Event()

    async def create_checkout(self, request: CheckoutRequest) -> Checkout:
        self.calls += 1
        self.started.set()
        await self.release.wait()
        return Checkout("mock", f"mock-{request.order_id}", f"http://pay/{request.checkout_token}")

    async def verify_webhook(self, payload: bytes, headers: Mapping[str, str]) -> PaymentEvent:
        raise AssertionError("not used")


class SupersedingProvider(BlockingProvider):
    def __init__(self) -> None:
        super().__init__()
        self.second_finished = asyncio.Event()

    async def create_checkout(self, request: CheckoutRequest) -> Checkout:
        self.calls += 1
        if self.calls == 1:
            self.started.set()
            await self.release.wait()
            suffix = "old"
        else:
            suffix = "winner"
            self.second_finished.set()
        return Checkout("mock", f"mock-{request.order_id}", f"http://pay/{suffix}")


async def _user(sessions: async_sessionmaker[AsyncSession]) -> User:
    async with sessions.begin() as session:
        user = User(telegram_user_id=uuid4().int % 10**12, first_name="Fictional")
        session.add(user)
        await session.flush()
        return user


async def test_ten_first_checkout_calls_have_one_provider_owner(
    payment_db: async_sessionmaker[AsyncSession], settings: Settings
) -> None:
    user = await _user(payment_db)
    provider, analytics = BlockingProvider(), Analytics()
    service = PaymentService(payment_db, ProductCatalog(settings), provider, analytics)
    owner = asyncio.create_task(service.create_checkout(user.id, "analysis_single"))
    await provider.started.wait()
    followers = await asyncio.gather(
        *(service.create_checkout(user.id, "analysis_single") for _ in range(9))
    )
    assert all(result.outcome is CheckoutOutcome.CREATING for result in followers)
    provider.release.set()
    assert (await owner).outcome is CheckoutOutcome.CREATED
    assert provider.calls == 1 and analytics.events == ["checkout_started"]
    async with payment_db() as session:
        orders = list((await session.scalars(select(PaymentOrder))).all())
        assert len(orders) == 1 and orders[0].status == "pending"
        assert orders[0].provider_checkout_id == f"mock-{orders[0].id}"


async def test_ten_payment_completions_credit_once(
    payment_db: async_sessionmaker[AsyncSession], settings: Settings
) -> None:
    user = await _user(payment_db)
    provider, analytics = BlockingProvider(), Analytics()
    provider.release.set()
    service = PaymentService(payment_db, ProductCatalog(settings), provider, analytics)
    checkout = await service.create_checkout(user.id, "analysis_single")
    assert checkout.checkout is not None
    event = PaymentEvent(
        "mock",
        "event-one",
        checkout.checkout.provider_checkout_id,
        "payment-one",
        "paid",
        settings.product_analysis_single_price_minor,
        settings.payment_currency,
    )
    outcomes = await asyncio.gather(*(service.complete(event) for _ in range(10)))
    assert outcomes.count(PaymentCompletionOutcome.COMPLETED) == 1
    assert outcomes.count(PaymentCompletionOutcome.ALREADY_COMPLETED) == 9
    assert analytics.events.count("purchase_completed") == 1
    async with payment_db() as session:
        purchases = await session.scalar(
            select(func.count())
            .select_from(CreditTransaction)
            .where(CreditTransaction.type == "purchase")
        )
        assert purchases == 1


async def test_stale_attempt_cannot_overwrite_winning_attempt(
    payment_db: async_sessionmaker[AsyncSession], settings: Settings
) -> None:
    user = await _user(payment_db)
    provider, analytics = SupersedingProvider(), Analytics()
    service = PaymentService(
        payment_db, ProductCatalog(settings), provider, analytics, creation_lease_seconds=-1
    )
    old = asyncio.create_task(service.create_checkout(user.id, "analysis_single"))
    await provider.started.wait()
    winner = await service.create_checkout(user.id, "analysis_single")
    assert winner.outcome is CheckoutOutcome.EXISTING
    provider.release.set()
    assert (await old).outcome is CheckoutOutcome.CREATING
    async with payment_db() as session:
        order = await session.scalar(select(PaymentOrder))
        assert order is not None and order.checkout_url == "http://pay/winner"
        assert order.status == "pending"
    assert provider.calls == 2 and analytics.events == ["checkout_started"]


async def test_duplicate_payment_and_event_ids_are_typed_mismatch(
    payment_db: async_sessionmaker[AsyncSession], settings: Settings
) -> None:
    first, second = await _user(payment_db), await _user(payment_db)
    provider, analytics = BlockingProvider(), Analytics()
    provider.release.set()
    service = PaymentService(payment_db, ProductCatalog(settings), provider, analytics)
    first_checkout = await service.create_checkout(first.id, "analysis_single")
    second_checkout = await service.create_checkout(second.id, "analysis_single")
    assert first_checkout.checkout and second_checkout.checkout
    amount, currency = settings.product_analysis_single_price_minor, settings.payment_currency
    first_event = PaymentEvent(
        "mock",
        "shared-event",
        first_checkout.checkout.provider_checkout_id,
        "shared-payment",
        "paid",
        amount,
        currency,
    )
    assert await service.complete(first_event) is PaymentCompletionOutcome.COMPLETED
    duplicate_payment = PaymentEvent(
        "mock",
        "other-event",
        second_checkout.checkout.provider_checkout_id,
        "shared-payment",
        "paid",
        amount,
        currency,
    )
    assert await service.complete(duplicate_payment) is PaymentCompletionOutcome.PAYMENT_MISMATCH
    duplicate_event = PaymentEvent(
        "mock",
        "shared-event",
        second_checkout.checkout.provider_checkout_id,
        "other-payment",
        "paid",
        amount,
        currency,
    )
    assert await service.complete(duplicate_event) is PaymentCompletionOutcome.PAYMENT_MISMATCH
