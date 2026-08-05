"""Transactional subscription lifecycle and exactly-once period accounting."""

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from enum import StrEnum
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.db.models import (
    BillingCustomer,
    BillingJob,
    BillingOutboxEvent,
    CreditTransaction,
    PaymentOrder,
    Subscription,
    User,
)
from app.db.subscription_models import SubscriptionPeriod

_ACTIVE_LIKE = (
    "incomplete",
    "active",
    "past_due",
    "cancel_at_period_end",
    "paused",
)


class SubscriptionLifecycleError(RuntimeError):
    """Safe subscription state failure without provider payload details."""


class SubscriptionStateMismatch(SubscriptionLifecycleError):
    """Provider identity or immutable commercial terms conflict with stored state."""


class SubscriptionOwnershipError(SubscriptionLifecycleError):
    """The subscription is not owned by the requested active user."""


class PeriodApplyOutcome(StrEnum):
    APPLIED = "applied"
    ALREADY_APPLIED = "already_applied"


class CancellationOutcome(StrEnum):
    UPDATED = "updated"
    ALREADY_TERMINAL = "already_terminal"
    NOT_FOUND = "not_found"


@dataclass(frozen=True)
class PaidSubscriptionPeriod:
    """Authoritative, provider-verified paid subscription period."""

    provider: str
    provider_customer_id: str
    provider_subscription_id: str
    provider_invoice_id: str
    provider_payment_id: str
    product_code: str
    product_version: int
    market: str
    currency: str
    amount_minor: int
    credits: int
    price_reference: str
    period_start: datetime
    period_end: datetime
    paid_at: datetime
    consent_version: str
    live_mode: bool | None = None


@dataclass(frozen=True)
class PastDueSubscriptionPeriod:
    provider: str
    provider_subscription_id: str
    provider_invoice_id: str
    product_code: str
    product_version: int
    currency: str
    amount_minor: int
    credits: int
    period_start: datetime
    period_end: datetime


def subscription_period_key(period_start: datetime, period_end: datetime) -> str:
    """Return a provider-independent canonical key for one UTC billing period."""
    start = _aware_utc(period_start)
    end = _aware_utc(period_end)
    if end <= start:
        raise SubscriptionLifecycleError("subscription period range is invalid")
    return f"{start.isoformat(timespec='seconds')}..{end.isoformat(timespec='seconds')}"


def _aware_utc(value: datetime) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise SubscriptionLifecycleError("subscription timestamps must be timezone-aware")
    return value.astimezone(UTC)


def _nonempty(value: str, name: str) -> str:
    normalized = value.strip()
    if not normalized:
        raise SubscriptionLifecycleError(f"{name} is required")
    return normalized


class SubscriptionLifecycleService:
    """Own subscription state; provider adapters only supply verified facts."""

    def __init__(self, sessions: async_sessionmaker[AsyncSession]) -> None:
        self._sessions = sessions

    async def apply_paid_period(
        self,
        user_id: UUID,
        value: PaidSubscriptionPeriod,
    ) -> PeriodApplyOutcome:
        """Grant one period exactly once under webhook/reconciliation concurrency."""
        self._validate_paid(value)
        period_start = _aware_utc(value.period_start)
        period_end = _aware_utc(value.period_end)
        paid_at = _aware_utc(value.paid_at)
        period_key = subscription_period_key(period_start, period_end)

        async with self._sessions.begin() as session:
            user = await session.scalar(select(User).where(User.id == user_id).with_for_update())
            if user is None or user.privacy_status != "active":
                raise SubscriptionOwnershipError("active user not found")

            customer = await session.scalar(
                select(BillingCustomer)
                .where(
                    BillingCustomer.user_id == user_id,
                    BillingCustomer.provider == value.provider,
                )
                .with_for_update()
            )
            if customer is None:
                customer = BillingCustomer(
                    user_id=user_id,
                    provider=value.provider,
                    provider_customer_id=value.provider_customer_id,
                )
                session.add(customer)
                await session.flush()
            elif customer.provider_customer_id not in {
                None,
                value.provider_customer_id,
            }:
                raise SubscriptionStateMismatch("provider customer identity mismatch")
            else:
                customer.provider_customer_id = value.provider_customer_id

            subscription = await session.scalar(
                select(Subscription)
                .where(
                    Subscription.provider == value.provider,
                    Subscription.provider_subscription_id == value.provider_subscription_id,
                )
                .with_for_update()
            )
            if subscription is None:
                active = await session.scalar(
                    select(Subscription)
                    .where(
                        Subscription.user_id == user_id,
                        Subscription.product_code == value.product_code,
                        Subscription.status.in_(_ACTIVE_LIKE),
                    )
                    .with_for_update()
                )
                if active is not None:
                    raise SubscriptionStateMismatch("active subscription identity mismatch")
                subscription = Subscription(
                    user_id=user_id,
                    billing_customer_id=customer.id,
                    provider=value.provider,
                    provider_subscription_id=value.provider_subscription_id,
                    product_code=value.product_code,
                    product_version=value.product_version,
                    status="active",
                    current_period_start=period_start,
                    current_period_end=period_end,
                    consent_version=value.consent_version,
                    consented_at=paid_at,
                )
                session.add(subscription)
                await session.flush()
                initial_period = True
            else:
                initial_period = subscription.last_order_id is None
                self._verify_subscription(subscription, user_id, customer.id, value)
                if (
                    subscription.status == "canceled"
                    or subscription.canceled_at is not None
                    or (
                        subscription.cancel_at is not None
                        and period_start >= _aware_utc(subscription.cancel_at)
                    )
                ):
                    raise SubscriptionStateMismatch(
                        "paid period is after subscription cancellation"
                    )

            period = await session.scalar(
                select(SubscriptionPeriod)
                .where(
                    SubscriptionPeriod.subscription_id == subscription.id,
                    SubscriptionPeriod.period_key == period_key,
                )
                .with_for_update()
            )
            if period is not None and period.status == "paid":
                self._verify_paid_period(period, value)
                return PeriodApplyOutcome.ALREADY_APPLIED
            if period is None:
                period = SubscriptionPeriod(
                    subscription_id=subscription.id,
                    provider=value.provider,
                    period_key=period_key,
                    status="pending",
                    period_start=period_start,
                    period_end=period_end,
                    credits=value.credits,
                    amount_minor=value.amount_minor,
                    currency=value.currency,
                    provider_invoice_id=value.provider_invoice_id,
                    provider_payment_id=value.provider_payment_id,
                    idempotency_key=f"subscription:period:{subscription.id}:{period_key}",
                )
                session.add(period)
                await session.flush()
            else:
                self._verify_pending_period(period, value)

            order = await session.scalar(
                select(PaymentOrder)
                .where(
                    PaymentOrder.provider == value.provider,
                    PaymentOrder.provider_payment_id == value.provider_payment_id,
                )
                .with_for_update()
            )
            if order is None:
                order = PaymentOrder(
                    user_id=user_id,
                    provider=value.provider,
                    product_code=value.product_code,
                    status="completed",
                    credits=value.credits,
                    amount_minor=value.amount_minor,
                    currency=value.currency,
                    mode="subscription_initial" if initial_period else "subscription_renewal",
                    market=value.market,
                    product_version=value.product_version,
                    billing_period=period_key,
                    provider_invoice_id=value.provider_invoice_id,
                    subscription_id=subscription.id,
                    provider_status="paid",
                    provider_payment_id=value.provider_payment_id,
                    idempotency_key=f"subscription:order:{subscription.id}:{period_key}",
                    commercial_snapshot={
                        "product_code": value.product_code,
                        "product_version": value.product_version,
                        "credits": value.credits,
                        "amount_minor": value.amount_minor,
                        "currency": value.currency,
                        "provider": value.provider,
                        "market": value.market,
                        "price_reference": value.price_reference,
                        "billing_period": "month",
                        "subscription_id": str(subscription.id),
                        "period_key": period_key,
                    },
                    completed_at=paid_at,
                    provider_live_mode=value.live_mode,
                )
                session.add(order)
                await session.flush()
            elif (
                order.user_id != user_id
                or order.subscription_id != subscription.id
                or order.billing_period != period_key
                or order.amount_minor != value.amount_minor
                or order.currency != value.currency
                or order.credits != value.credits
            ):
                raise SubscriptionStateMismatch("provider payment identity mismatch")

            transaction = await session.scalar(
                select(CreditTransaction)
                .where(
                    CreditTransaction.idempotency_key
                    == f"subscription:credit:{subscription.id}:{period_key}"
                )
                .with_for_update()
            )
            if transaction is None:
                transaction = CreditTransaction(
                    user_id=user_id,
                    type="purchase",
                    amount=value.credits,
                    idempotency_key=f"subscription:credit:{subscription.id}:{period_key}",
                    payment_order_id=order.id,
                    product_code=value.product_code,
                    external_payment_provider=value.provider,
                    external_payment_id=value.provider_payment_id,
                )
                session.add(transaction)
                await session.flush()
            elif (
                transaction.user_id != user_id
                or transaction.payment_order_id != order.id
                or transaction.amount != value.credits
            ):
                raise SubscriptionStateMismatch("subscription credit identity mismatch")

            period.status = "paid"
            period.provider_invoice_id = value.provider_invoice_id
            period.provider_payment_id = value.provider_payment_id
            period.payment_order_id = order.id
            period.purchase_transaction_id = transaction.id
            period.paid_at = paid_at

            preserve_cancellation = bool(
                subscription.status == "cancel_at_period_end"
                and subscription.cancel_at is not None
                and period_end <= _aware_utc(subscription.cancel_at)
            )
            if not preserve_cancellation:
                subscription.status = "active"
                subscription.cancel_at = None
            subscription.current_period_start = period_start
            subscription.current_period_end = period_end
            subscription.last_order_id = order.id
            session.add(
                BillingOutboxEvent(
                    aggregate_type="subscription",
                    aggregate_id=str(subscription.id),
                    event_type=(
                        "subscription_activated" if initial_period else "subscription_renewed"
                    ),
                    payload={
                        "product_code": value.product_code,
                        "period_end": period_end.isoformat(),
                        "credits": value.credits,
                    },
                    idempotency_key=f"subscription_period_paid:{period.id}",
                )
            )
            return PeriodApplyOutcome.APPLIED

    async def mark_past_due(self, value: PastDueSubscriptionPeriod) -> bool:
        """Record an authoritative unpaid period without granting credits."""
        self._validate_past_due(value)
        period_start = _aware_utc(value.period_start)
        period_end = _aware_utc(value.period_end)
        period_key = subscription_period_key(period_start, period_end)

        async with self._sessions() as lookup:
            identified = await lookup.scalar(
                select(Subscription).where(
                    Subscription.provider == value.provider,
                    Subscription.provider_subscription_id == value.provider_subscription_id,
                )
            )
            if identified is None:
                return False
            user_id = identified.user_id

        async with self._sessions.begin() as session:
            user = await session.scalar(select(User).where(User.id == user_id).with_for_update())
            subscription = await session.scalar(
                select(Subscription).where(Subscription.id == identified.id).with_for_update()
            )
            if user is None or subscription is None or user.privacy_status != "active":
                return False
            if subscription.status in {"canceled", "unpaid"}:
                return False
            if (
                subscription.product_code != value.product_code
                or subscription.product_version != value.product_version
            ):
                raise SubscriptionStateMismatch("past-due commercial identity mismatch")

            period = await session.scalar(
                select(SubscriptionPeriod)
                .where(
                    SubscriptionPeriod.subscription_id == subscription.id,
                    SubscriptionPeriod.period_key == period_key,
                )
                .with_for_update()
            )
            if period is not None and period.status == "paid":
                return False
            if period is not None and period.status == "past_due":
                if (
                    period.provider_invoice_id == value.provider_invoice_id
                    and period.amount_minor == value.amount_minor
                    and period.currency == value.currency
                    and period.credits == value.credits
                ):
                    return False
                raise SubscriptionStateMismatch("past-due period identity mismatch")
            if period is None:
                period = SubscriptionPeriod(
                    subscription_id=subscription.id,
                    provider=value.provider,
                    period_key=period_key,
                    status="past_due",
                    period_start=period_start,
                    period_end=period_end,
                    credits=value.credits,
                    amount_minor=value.amount_minor,
                    currency=value.currency,
                    provider_invoice_id=value.provider_invoice_id,
                    idempotency_key=f"subscription:period:{subscription.id}:{period_key}",
                )
                session.add(period)
            else:
                period.status = "past_due"
                period.provider_invoice_id = value.provider_invoice_id
            if subscription.status != "cancel_at_period_end":
                subscription.status = "past_due"
            subscription.current_period_start = period_start
            subscription.current_period_end = period_end
            session.add(
                BillingOutboxEvent(
                    aggregate_type="subscription",
                    aggregate_id=str(subscription.id),
                    event_type="subscription_payment_failed",
                    payload={"product_code": subscription.product_code},
                    idempotency_key=f"subscription_past_due:{subscription.id}:{period_key}",
                )
            )
            return True

    async def record_cancel_at_period_end(
        self,
        user_id: UUID,
        subscription_id: UUID,
        effective_at: datetime,
    ) -> CancellationOutcome:
        """Persist provider-confirmed cancellation without removing paid credits."""
        cancel_at = _aware_utc(effective_at)
        async with self._sessions.begin() as session:
            user = await session.scalar(select(User).where(User.id == user_id).with_for_update())
            subscription = await session.scalar(
                select(Subscription).where(Subscription.id == subscription_id).with_for_update()
            )
            if (
                user is None
                or subscription is None
                or subscription.user_id != user_id
                or user.privacy_status != "active"
            ):
                return CancellationOutcome.NOT_FOUND
            if subscription.status in {"canceled", "unpaid"}:
                return CancellationOutcome.ALREADY_TERMINAL
            if subscription.current_period_end is None or cancel_at < _aware_utc(
                subscription.current_period_end
            ):
                raise SubscriptionStateMismatch("cancellation precedes paid period end")
            if (
                subscription.status == "cancel_at_period_end"
                and subscription.cancel_at == cancel_at
            ):
                return CancellationOutcome.ALREADY_TERMINAL
            subscription.status = "cancel_at_period_end"
            subscription.cancel_at = cancel_at
            session.add(
                BillingOutboxEvent(
                    aggregate_type="subscription",
                    aggregate_id=str(subscription.id),
                    event_type="subscription_cancel_scheduled",
                    payload={"effective_at": cancel_at.isoformat()},
                    idempotency_key=f"subscription_cancel_scheduled:{subscription.id}:{cancel_at.isoformat()}",
                )
            )
            return CancellationOutcome.UPDATED

    async def record_resumed(
        self,
        user_id: UUID,
        subscription_id: UUID,
        now: datetime | None = None,
    ) -> CancellationOutcome:
        """Persist provider-confirmed restoration before cancellation becomes effective."""
        current = _aware_utc(now or datetime.now(UTC))
        async with self._sessions.begin() as session:
            user = await session.scalar(select(User).where(User.id == user_id).with_for_update())
            subscription = await session.scalar(
                select(Subscription).where(Subscription.id == subscription_id).with_for_update()
            )
            if (
                user is None
                or subscription is None
                or subscription.user_id != user_id
                or user.privacy_status != "active"
            ):
                return CancellationOutcome.NOT_FOUND
            if subscription.status != "cancel_at_period_end":
                return CancellationOutcome.ALREADY_TERMINAL
            if subscription.cancel_at is None or current >= _aware_utc(subscription.cancel_at):
                return CancellationOutcome.ALREADY_TERMINAL
            subscription.status = "active"
            subscription.cancel_at = None
            session.add(
                BillingOutboxEvent(
                    aggregate_type="subscription",
                    aggregate_id=str(subscription.id),
                    event_type="subscription_resumed",
                    payload={"product_code": subscription.product_code},
                    idempotency_key=f"subscription_resumed:{subscription.id}:{current.isoformat()}",
                )
            )
            return CancellationOutcome.UPDATED

    async def enqueue_due_renewals(
        self,
        now: datetime | None = None,
        lookahead: timedelta = timedelta(minutes=15),
    ) -> int:
        """Create one durable renewal/reconciliation job per upcoming period boundary."""
        current = _aware_utc(now or datetime.now(UTC))
        if lookahead < timedelta(0):
            raise SubscriptionLifecycleError("renewal lookahead cannot be negative")
        cutoff = current + lookahead
        created = 0
        async with self._sessions.begin() as session:
            subscriptions = list(
                (
                    await session.scalars(
                        select(Subscription)
                        .where(
                            Subscription.status.in_(("active", "past_due")),
                            Subscription.current_period_end.is_not(None),
                            Subscription.current_period_end <= cutoff,
                        )
                        .order_by(Subscription.current_period_end, Subscription.id)
                        .with_for_update(skip_locked=True)
                    )
                ).all()
            )
            for subscription in subscriptions:
                assert subscription.current_period_end is not None
                boundary = _aware_utc(subscription.current_period_end)
                key = f"subscription:renewal:{subscription.id}:{boundary.isoformat()}"
                result = await session.execute(
                    pg_insert(BillingJob)
                    .values(
                        job_type="subscription_renewal",
                        provider=subscription.provider,
                        object_type="subscription",
                        object_id=str(subscription.id),
                        idempotency_key=key,
                        status="pending",
                        available_at=max(current, boundary),
                    )
                    .on_conflict_do_nothing(index_elements=["idempotency_key"])
                )
                if result.rowcount:
                    created += 1
        return created

    async def finalize_terminal_states(
        self,
        now: datetime | None = None,
        grace_period: timedelta = timedelta(days=3),
    ) -> tuple[int, int]:
        """Finalize elapsed cancellations and grace periods idempotently."""
        current = _aware_utc(now or datetime.now(UTC))
        if grace_period < timedelta(0):
            raise SubscriptionLifecycleError("grace period cannot be negative")
        canceled = unpaid = 0
        async with self._sessions.begin() as session:
            subscriptions = list(
                (
                    await session.scalars(
                        select(Subscription)
                        .where(
                            Subscription.status.in_(("cancel_at_period_end", "past_due")),
                            Subscription.current_period_end.is_not(None),
                        )
                        .order_by(Subscription.current_period_end, Subscription.id)
                        .with_for_update(skip_locked=True)
                    )
                ).all()
            )
            for subscription in subscriptions:
                assert subscription.current_period_end is not None
                period_end = _aware_utc(subscription.current_period_end)
                if subscription.status == "cancel_at_period_end" and period_end <= current:
                    subscription.status = "canceled"
                    subscription.canceled_at = current
                    canceled += 1
                    session.add(
                        BillingOutboxEvent(
                            aggregate_type="subscription",
                            aggregate_id=str(subscription.id),
                            event_type="subscription_canceled",
                            payload={"product_code": subscription.product_code},
                            idempotency_key=f"subscription_canceled:{subscription.id}:{period_end.isoformat()}",
                        )
                    )
                elif subscription.status == "past_due" and period_end + grace_period <= current:
                    subscription.status = "unpaid"
                    unpaid += 1
                    session.add(
                        BillingOutboxEvent(
                            aggregate_type="subscription",
                            aggregate_id=str(subscription.id),
                            event_type="subscription_unpaid",
                            payload={"product_code": subscription.product_code},
                            idempotency_key=f"subscription_unpaid:{subscription.id}:{period_end.isoformat()}",
                        )
                    )
        return canceled, unpaid

    @staticmethod
    def _validate_paid(value: PaidSubscriptionPeriod) -> None:
        for field, name in (
            (value.provider, "provider"),
            (value.provider_customer_id, "provider customer id"),
            (value.provider_subscription_id, "provider subscription id"),
            (value.provider_invoice_id, "provider invoice id"),
            (value.provider_payment_id, "provider payment id"),
            (value.product_code, "product code"),
            (value.market, "market"),
            (value.currency, "currency"),
            (value.price_reference, "price reference"),
            (value.consent_version, "consent version"),
        ):
            _nonempty(field, name)
        if value.product_version < 1 or value.amount_minor <= 0 or value.credits <= 0:
            raise SubscriptionLifecycleError("subscription commercial values are invalid")
        if len(value.currency) != 3 or not value.currency.isupper():
            raise SubscriptionLifecycleError("subscription currency is invalid")
        subscription_period_key(value.period_start, value.period_end)
        _aware_utc(value.paid_at)

    @staticmethod
    def _validate_past_due(value: PastDueSubscriptionPeriod) -> None:
        for field, name in (
            (value.provider, "provider"),
            (value.provider_subscription_id, "provider subscription id"),
            (value.provider_invoice_id, "provider invoice id"),
            (value.product_code, "product code"),
            (value.currency, "currency"),
        ):
            _nonempty(field, name)
        if value.product_version < 1 or value.amount_minor <= 0 or value.credits <= 0:
            raise SubscriptionLifecycleError("subscription commercial values are invalid")
        subscription_period_key(value.period_start, value.period_end)

    @staticmethod
    def _verify_subscription(
        subscription: Subscription,
        user_id: UUID,
        customer_id: UUID,
        value: PaidSubscriptionPeriod,
    ) -> None:
        if (
            subscription.user_id != user_id
            or subscription.billing_customer_id != customer_id
            or subscription.product_code != value.product_code
            or subscription.product_version != value.product_version
        ):
            raise SubscriptionStateMismatch("subscription identity mismatch")

    @staticmethod
    def _verify_paid_period(
        period: SubscriptionPeriod,
        value: PaidSubscriptionPeriod,
    ) -> None:
        if (
            period.provider_invoice_id != value.provider_invoice_id
            or period.provider_payment_id != value.provider_payment_id
            or period.amount_minor != value.amount_minor
            or period.currency != value.currency
            or period.credits != value.credits
        ):
            raise SubscriptionStateMismatch("paid period identity mismatch")

    @staticmethod
    def _verify_pending_period(
        period: SubscriptionPeriod,
        value: PaidSubscriptionPeriod,
    ) -> None:
        if (
            period.status not in {"pending", "past_due", "failed"}
            or period.amount_minor != value.amount_minor
            or period.currency != value.currency
            or period.credits != value.credits
            or period.provider_invoice_id not in {None, value.provider_invoice_id}
            or period.provider_payment_id not in {None, value.provider_payment_id}
        ):
            raise SubscriptionStateMismatch("pending period identity mismatch")
