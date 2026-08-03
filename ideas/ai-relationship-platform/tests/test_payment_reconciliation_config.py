from typing import Any, cast

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.services.payment_reconciliation_service import PaymentReconciliationSweeper


def test_empty_supported_provider_set_means_none_supported() -> None:
    sessions = cast(async_sessionmaker[AsyncSession], cast(Any, object()))
    sweeper = PaymentReconciliationSweeper(sessions, 60, set())
    assert not sweeper.supports_provider("stripe")
    assert not sweeper.supports_provider("yookassa")
    assert not sweeper.supports_provider("mock")


def test_none_supported_provider_set_uses_explicit_production_defaults() -> None:
    sessions = cast(async_sessionmaker[AsyncSession], cast(Any, object()))
    sweeper = PaymentReconciliationSweeper(sessions, 60, None)
    assert sweeper.supports_provider("stripe")
    assert sweeper.supports_provider("yookassa")
    assert not sweeper.supports_provider("mock")
