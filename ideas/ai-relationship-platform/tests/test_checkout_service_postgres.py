"""Real PostgreSQL checkout creation concurrency."""

import asyncio
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.config import Settings
from app.db.models import PaymentOrder, User
from app.domain.billing import BillingCatalog
from app.providers.payments.base import PaymentProviderName, UnknownProviderOutcome
from app.providers.payments.gateway import CreateCheckout, HostedCheckout
from app.services.checkout_service import CheckoutService

pytestmark = pytest.mark.postgres
pytest_plugins = ("tests.payment_postgres_helpers",)


class IdempotentGateway:
    def __init__(self, timeout_once: bool = False) -> None:
        self.keys: list[str] = []
        self.timeout_once = timeout_once

    async def create_checkout(self, request: CreateCheckout) -> HostedCheckout:
        self.keys.append(request.idempotency_key)
        if self.timeout_once:
            self.timeout_once = False
            raise UnknownProviderOutcome
        return HostedCheckout("yk-one", "https://provider.test/one", "pending")

    async def fetch_payment(self, checkout_id: str):  # type: ignore[no-untyped-def]
        raise AssertionError


async def _user(sessions: async_sessionmaker[AsyncSession]) -> User:
    async with sessions.begin() as session:
        user = User(telegram_user_id=99112233, first_name="Checkout")
        session.add(user)
        await session.flush()
        return user


def _settings(settings: Settings) -> Settings:
    return settings.model_copy(
        update={
            "billing_enabled": True,
            "yookassa_enabled": True,
            "yookassa_receipts_required": True,
            "checkout_creation_lease_seconds": 1,
        }
    )


async def test_ten_checkout_requests_create_one_order_and_provider_checkout(
    payment_db: async_sessionmaker[AsyncSession],
    settings: Settings,
) -> None:
    user = await _user(payment_db)
    gateway = IdempotentGateway()
    configured = _settings(settings)
    service = CheckoutService(
        payment_db, configured, BillingCatalog(configured), {PaymentProviderName.YOOKASSA: gateway}
    )
    results = await asyncio.gather(
        *(
            service.create_one_time_checkout(
                user.id, "analysis_single", "RU", "RUB", receipt_contact="buyer@example.com"
            )
            for _ in range(10)
        )
    )
    async with payment_db() as session:
        count = await session.scalar(select(func.count()).select_from(PaymentOrder))
        order = await session.scalar(select(PaymentOrder))
    assert count == 1 and len(gateway.keys) == 1
    assert order is not None and gateway.keys == [order.idempotency_key]
    assert sum(result.url == "https://provider.test/one" for result in results) >= 1
    assert all(result.url in {None, "https://provider.test/one"} for result in results)
    assert order.encrypted_receipt_contact is None


async def test_ambiguous_checkout_recovers_with_same_key_and_encrypted_contact(
    payment_db: async_sessionmaker[AsyncSession],
    settings: Settings,
) -> None:
    user = await _user(payment_db)
    gateway = IdempotentGateway(timeout_once=True)
    configured = _settings(settings)
    service = CheckoutService(
        payment_db, configured, BillingCatalog(configured), {PaymentProviderName.YOOKASSA: gateway}
    )
    first = await service.create_one_time_checkout(
        user.id, "analysis_single", "RU", "RUB", receipt_contact="buyer@example.com"
    )
    async with payment_db.begin() as session:
        order = await session.get(PaymentOrder, first.order_id)
        assert order is not None and order.encrypted_receipt_contact is not None
        assert b"buyer@example.com" not in order.encrypted_receipt_contact
        assert "buyer@example.com" not in str(order.commercial_snapshot)
        order.checkout_creation_started_at = datetime.now(UTC) - timedelta(seconds=5)
    second = await service.create_one_time_checkout(
        user.id, "analysis_single", "RU", "RUB", receipt_contact="buyer@example.com"
    )
    assert second.url == "https://provider.test/one"
    assert gateway.keys[0] == gateway.keys[1]
    async with payment_db() as session:
        order = await session.get(PaymentOrder, first.order_id)
        assert order is not None and order.encrypted_receipt_contact is None
