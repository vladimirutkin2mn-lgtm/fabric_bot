"""Durable checkout creation and exactly-once payment completion."""

import asyncio
import logging
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum
from typing import cast
from uuid import UUID, uuid4

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.db.models import CreditTransaction, PaymentOrder, User
from app.domain.products import ProductCatalog
from app.providers.analytics import AnalyticsClient
from app.providers.payments.base import (
    Checkout,
    CheckoutRequest,
    PaymentEvent,
    PaymentProvider,
)

logger = logging.getLogger(__name__)


class CheckoutOutcome(StrEnum):
    CREATED = "created"
    EXISTING = "existing"
    PROVIDER_FAILED = "provider_failed"
    UNKNOWN_PRODUCT = "unknown_product"
    USER_NOT_FOUND = "user_not_found"
    CREATING = "creating"


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


PaymentTracking = tuple[UUID, int, str]


class PaymentService:
    def __init__(
        self,
        sessions: async_sessionmaker[AsyncSession],
        catalog: ProductCatalog,
        provider: PaymentProvider,
        analytics: AnalyticsClient,
        provider_name: str = "mock",
        creation_lease_seconds: int = 60,
    ) -> None:
        self._sessions, self._catalog, self._provider = sessions, catalog, provider
        self._analytics, self._provider_name = analytics, provider_name
        self._creation_lease_seconds = creation_lease_seconds

    async def create_checkout(self, user_id: UUID, product_code: str) -> CheckoutResult:
        product = self._catalog.get(product_code)
        if product is None:
            return CheckoutResult(CheckoutOutcome.UNKNOWN_PRODUCT)

        created = False
        attempt_id = uuid4()
        now = datetime.now(UTC)
        async with self._sessions.begin() as session:
            user = await session.scalar(select(User).where(User.id == user_id).with_for_update())
            if user is None:
                return CheckoutResult(CheckoutOutcome.USER_NOT_FOUND)

            order = await session.scalar(
                select(PaymentOrder)
                .where(
                    PaymentOrder.user_id == user_id,
                    PaymentOrder.provider == self._provider_name,
                    PaymentOrder.product_code == product.code.value,
                    PaymentOrder.status.in_(("creating", "pending")),
                )
                .with_for_update()
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
                    checkout_creation_attempt_id=attempt_id,
                    checkout_creation_started_at=now,
                )
                session.add(order)
                await session.flush()
                created = True
            elif order.status == "pending":
                if order.provider_checkout_id and order.checkout_url:
                    return CheckoutResult(
                        CheckoutOutcome.EXISTING,
                        order.id,
                        Checkout(
                            order.provider,
                            order.provider_checkout_id,
                            order.checkout_url,
                        ),
                    )
                return CheckoutResult(CheckoutOutcome.PROVIDER_FAILED, order.id)
            else:
                started_at = order.checkout_creation_started_at or order.updated_at
                age = (now - started_at).total_seconds()
                if age <= self._creation_lease_seconds:
                    return CheckoutResult(CheckoutOutcome.CREATING, order.id)
                order.checkout_creation_attempt_id = attempt_id
                order.checkout_creation_started_at = now

            request = CheckoutRequest(
                order.id,
                order.checkout_token,
                order.product_code,
                order.amount_minor,
                order.currency,
            )

        try:
            checkout = await self._provider.create_checkout(request)
        except asyncio.CancelledError:
            await asyncio.shield(self._mark_creation_failed(request.order_id, attempt_id))
            raise
        except Exception:
            await self._mark_creation_failed(request.order_id, attempt_id)
            logger.warning(
                "checkout_creation_failed order_id=%s provider=%s",
                request.order_id,
                self._provider_name,
            )
            return CheckoutResult(CheckoutOutcome.PROVIDER_FAILED, request.order_id)

        saved, should_track = await self._persist_checkout_success(
            request.order_id, attempt_id, checkout
        )
        if not saved:
            return CheckoutResult(CheckoutOutcome.CREATING, request.order_id)
        if should_track:
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

    async def _persist_checkout_success(
        self, order_id: UUID, attempt_id: UUID, checkout: Checkout
    ) -> tuple[bool, bool]:
        async with self._sessions.begin() as session:
            current = await session.get(PaymentOrder, order_id, with_for_update=True)
            if (
                current is None
                or current.status != "creating"
                or current.checkout_creation_attempt_id != attempt_id
            ):
                return False, False
            should_track = not current.checkout_started_emitted
            current.provider_checkout_id = checkout.provider_checkout_id
            current.checkout_url = checkout.url
            current.status = "pending"
            current.checkout_started_emitted = True
            return True, should_track

    async def _mark_creation_failed(self, order_id: UUID, attempt_id: UUID) -> bool:
        async with self._sessions.begin() as session:
            current = await session.get(PaymentOrder, order_id, with_for_update=True)
            if (
                current is None
                or current.status != "creating"
                or current.checkout_creation_attempt_id != attempt_id
            ):
                return False
            current.status = "failed"
            return True

    async def complete(self, event: PaymentEvent) -> PaymentCompletionOutcome:
        try:
            outcome, tracking = await self._complete_transaction(event)
        except IntegrityError as exc:
            if not self._is_payment_identity_conflict(exc):
                raise
            logger.warning("payment_completion_conflict provider=%s", event.provider)
            return PaymentCompletionOutcome.PAYMENT_MISMATCH

        if tracking is not None:
            user_id, credits, product_code = tracking
            await self._track(
                user_id,
                "purchase_completed",
                {
                    "product_code": product_code,
                    "provider": event.provider,
                    "credits": str(credits),
                },
            )
        return outcome

    async def _complete_transaction(
        self, event: PaymentEvent
    ) -> tuple[PaymentCompletionOutcome, PaymentTracking | None]:
        async with self._sessions.begin() as session:
            order = await session.scalar(
                select(PaymentOrder)
                .where(PaymentOrder.provider_checkout_id == event.checkout_id)
                .with_for_update()
            )
            if order is None:
                return PaymentCompletionOutcome.ORDER_NOT_FOUND, None
            if order.provider != event.provider:
                return PaymentCompletionOutcome.PROVIDER_MISMATCH, None
            if order.status == "completed":
                outcome = (
                    PaymentCompletionOutcome.ALREADY_COMPLETED
                    if order.provider_payment_id == event.payment_id
                    else PaymentCompletionOutcome.PAYMENT_MISMATCH
                )
                return outcome, None
            if event.status != "paid":
                return PaymentCompletionOutcome.PAYMENT_FAILED, None
            if order.amount_minor != event.amount_minor:
                return PaymentCompletionOutcome.AMOUNT_MISMATCH, None
            if order.currency != event.currency:
                return PaymentCompletionOutcome.CURRENCY_MISMATCH, None
            if order.provider_checkout_id != event.checkout_id:
                return PaymentCompletionOutcome.CHECKOUT_MISMATCH, None

            payment_owner = await session.scalar(
                select(PaymentOrder.id).where(
                    PaymentOrder.provider_payment_id == event.payment_id,
                    PaymentOrder.id != order.id,
                )
            )
            event_owner = await session.scalar(
                select(PaymentOrder.id).where(
                    PaymentOrder.provider_event_id == event.event_id,
                    PaymentOrder.id != order.id,
                )
            )
            ledger_owner = await session.scalar(
                select(CreditTransaction.id).where(
                    CreditTransaction.external_payment_id == event.payment_id,
                    CreditTransaction.payment_order_id != order.id,
                )
            )
            if payment_owner is not None or event_owner is not None or ledger_owner is not None:
                return PaymentCompletionOutcome.PAYMENT_MISMATCH, None

            order.status = "completed"
            order.completed_at = datetime.now(UTC)
            order.provider_payment_id = event.payment_id
            order.provider_event_id = event.event_id
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
            return (
                PaymentCompletionOutcome.COMPLETED,
                (order.user_id, order.credits, order.product_code),
            )

    @staticmethod
    def _is_payment_identity_conflict(exc: IntegrityError) -> bool:
        constraint_name = getattr(getattr(exc.orig, "diag", None), "constraint_name", None)
        return constraint_name in {
            "payment_orders_provider_payment_id_key",
            "payment_orders_provider_event_id_key",
            "credit_transactions_external_payment_id_key",
            "credit_transactions_payment_order_id_key",
            "credit_transactions_idempotency_key_key",
        }

    async def order_by_token(self, token: UUID) -> PaymentOrder | None:
        async with self._sessions() as session:
            return cast(
                PaymentOrder | None,
                await session.scalar(
                    select(PaymentOrder).where(PaymentOrder.checkout_token == token)
                ),
            )

    async def _track(self, user_id: UUID, event: str, properties: dict[str, str]) -> None:
        try:
            await self._analytics.track(str(user_id), event, properties)
        except Exception:
            logger.warning("payment_analytics_failed event=%s", event)
