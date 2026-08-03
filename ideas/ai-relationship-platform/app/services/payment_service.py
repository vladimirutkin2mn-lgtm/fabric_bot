"""Durable checkout creation and exactly-once payment completion."""

import logging
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.db.models import CreditTransaction, PaymentOrder, User
from app.domain.products import ProductCatalog
from app.providers.analytics import AnalyticsClient
from app.providers.payments.base import Checkout, CheckoutRequest, PaymentEvent, PaymentProvider

logger = logging.getLogger(__name__)


class CheckoutOutcome(StrEnum):
    CREATED = "created"
    EXISTING = "existing"
    PROVIDER_FAILED = "provider_failed"
    UNKNOWN_PRODUCT = "unknown_product"
    USER_NOT_FOUND = "user_not_found"


class PaymentCompletionOutcome(StrEnum):
    COMPLETED = "completed"
    ALREADY_COMPLETED = "already_completed"
    ORDER_NOT_FOUND = "order_not_found"
    PROVIDER_MISMATCH = "provider_mismatch"
    AMOUNT_MISMATCH = "amount_mismatch"
    CURRENCY_MISMATCH = "currency_mismatch"
    CHECKOUT_MISMATCH = "checkout_mismatch"
    PAYMENT_MISMATCH = "payment_mismatch"
    PAYMENT_FAILED = "payment_failed"


@dataclass(frozen=True)
class CheckoutResult:
    outcome: CheckoutOutcome
    order_id: UUID | None = None
    checkout: Checkout | None = None


class PaymentService:
    def __init__(
        self,
        sessions: async_sessionmaker[AsyncSession],
        catalog: ProductCatalog,
        provider: PaymentProvider,
        analytics: AnalyticsClient,
        provider_name: str = "mock",
    ) -> None:
        self._sessions, self._catalog, self._provider = sessions, catalog, provider
        self._analytics, self._provider_name = analytics, provider_name

    async def create_checkout(self, user_id: UUID, product_code: str) -> CheckoutResult:
        product = self._catalog.get(product_code)
        if product is None:
            return CheckoutResult(CheckoutOutcome.UNKNOWN_PRODUCT)
        created = False
        async with self._sessions.begin() as session:
            user = await session.scalar(select(User).where(User.id == user_id).with_for_update())
            if user is None:
                return CheckoutResult(CheckoutOutcome.USER_NOT_FOUND)
            order = await session.scalar(
                select(PaymentOrder).where(
                    PaymentOrder.user_id == user_id,
                    PaymentOrder.provider == self._provider_name,
                    PaymentOrder.product_code == product.code.value,
                    PaymentOrder.status.in_(("creating", "pending")),
                )
            )
            if order is None:
                order = PaymentOrder(
                    user_id=user_id,
                    provider=self._provider_name,
                    product_code=product.code.value,
                    status="creating",
                    credits=product.credits,
                    amount_minor=product.amount_minor,
                    currency=product.currency,
                )
                session.add(order)
                await session.flush()
                created = True
            elif order.status == "pending" and order.provider_checkout_id:
                request = CheckoutRequest(
                    order.id,
                    order.checkout_token,
                    order.product_code,
                    order.amount_minor,
                    order.currency,
                )
            else:
                request = CheckoutRequest(
                    order.id,
                    order.checkout_token,
                    order.product_code,
                    order.amount_minor,
                    order.currency,
                )
        try:
            checkout = await self._provider.create_checkout(request)
        except Exception:
            async with self._sessions.begin() as session:
                current = await session.get(PaymentOrder, request.order_id, with_for_update=True)
                if current and current.status == "creating":
                    current.status = "failed"
            logger.warning(
                "checkout_creation_failed order_id=%s provider=%s",
                request.order_id,
                self._provider_name,
            )
            return CheckoutResult(CheckoutOutcome.PROVIDER_FAILED, request.order_id)
        async with self._sessions.begin() as session:
            current = await session.get(PaymentOrder, request.order_id, with_for_update=True)
            if current is None:
                return CheckoutResult(CheckoutOutcome.PROVIDER_FAILED, request.order_id)
            current.provider_checkout_id, current.status = checkout.provider_checkout_id, "pending"
        if created:
            await self._track(
                user_id,
                "checkout_started",
                {
                    "product_code": product.code.value,
                    "provider": self._provider_name,
                    "credits": str(product.credits),
                },
            )
        return CheckoutResult(
            CheckoutOutcome.CREATED if created else CheckoutOutcome.EXISTING,
            request.order_id,
            checkout,
        )

    async def complete(self, event: PaymentEvent) -> PaymentCompletionOutcome:
        completed_user: UUID | None = None
        credits = 0
        product_code = ""
        async with self._sessions.begin() as session:
            order = await session.scalar(
                select(PaymentOrder)
                .where(PaymentOrder.provider_checkout_id == event.checkout_id)
                .with_for_update()
            )
            if order is None:
                return PaymentCompletionOutcome.ORDER_NOT_FOUND
            if order.provider != event.provider:
                return PaymentCompletionOutcome.PROVIDER_MISMATCH
            if order.status == "completed":
                return (
                    PaymentCompletionOutcome.ALREADY_COMPLETED
                    if order.provider_payment_id == event.payment_id
                    else PaymentCompletionOutcome.PAYMENT_MISMATCH
                )
            if event.status != "paid":
                return PaymentCompletionOutcome.PAYMENT_FAILED
            if order.amount_minor != event.amount_minor:
                return PaymentCompletionOutcome.AMOUNT_MISMATCH
            if order.currency != event.currency:
                return PaymentCompletionOutcome.CURRENCY_MISMATCH
            if order.provider_checkout_id != event.checkout_id:
                return PaymentCompletionOutcome.CHECKOUT_MISMATCH
            order.status, order.completed_at = "completed", datetime.now(UTC)
            order.provider_payment_id, order.provider_event_id = event.payment_id, event.event_id
            session.add(
                CreditTransaction(
                    user_id=order.user_id,
                    type="purchase",
                    amount=order.credits,
                    idempotency_key=f"purchase:{order.id}",
                    payment_order_id=order.id,
                    product_code=order.product_code,
                    external_payment_id=event.payment_id,
                )
            )
            completed_user, credits, product_code = order.user_id, order.credits, order.product_code
        if completed_user is not None:
            await self._track(
                completed_user,
                "purchase_completed",
                {"product_code": product_code, "provider": event.provider, "credits": str(credits)},
            )
        return PaymentCompletionOutcome.COMPLETED

    async def order_by_token(self, token: UUID) -> PaymentOrder | None:
        async with self._sessions() as session:
            return await session.scalar(
                select(PaymentOrder).where(PaymentOrder.checkout_token == token)
            )

    async def _track(self, user_id: UUID, event: str, properties: dict[str, str]) -> None:
        try:
            await self._analytics.track(str(user_id), event, properties)
        except Exception:
            logger.warning("payment_analytics_failed event=%s", event)
