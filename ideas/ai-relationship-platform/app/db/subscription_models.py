"""Durable subscription-period accounting models."""

from datetime import datetime
from uuid import UUID, uuid4

from sqlalchemy import (
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    UniqueConstraint,
    func,
)
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base


class SubscriptionPeriod(Base):
    """One authoritative provider billing period and its exactly-once credit grant."""

    __tablename__ = "subscription_periods"
    __table_args__ = (
        UniqueConstraint(
            "subscription_id",
            "period_key",
            name="uq_subscription_period_key",
        ),
        UniqueConstraint(
            "provider",
            "provider_invoice_id",
            name="uq_subscription_period_provider_invoice",
        ),
        UniqueConstraint(
            "provider",
            "provider_payment_id",
            name="uq_subscription_period_provider_payment",
        ),
        CheckConstraint(
            "status IN ('pending','paid','past_due','failed','canceled','manual_review')",
            name="ck_subscription_periods_status",
        ),
        CheckConstraint(
            "credits > 0 AND amount_minor > 0",
            name="ck_subscription_periods_positive",
        ),
        CheckConstraint(
            "char_length(currency) = 3",
            name="ck_subscription_periods_currency",
        ),
        CheckConstraint(
            "period_end > period_start",
            name="ck_subscription_periods_range",
        ),
        CheckConstraint(
            "(status = 'paid') = "
            "(paid_at IS NOT NULL AND payment_order_id IS NOT NULL "
            "AND purchase_transaction_id IS NOT NULL AND provider_payment_id IS NOT NULL)",
            name="ck_subscription_periods_paid_refs",
        ),
        Index(
            "ix_subscription_periods_subscription_end",
            "subscription_id",
            "period_end",
        ),
        Index(
            "ix_subscription_periods_status_end",
            "status",
            "period_end",
        ),
    )

    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)
    subscription_id: Mapped[UUID] = mapped_column(
        ForeignKey("subscriptions.id", ondelete="RESTRICT"),
        index=True,
    )
    provider: Mapped[str] = mapped_column(String(32))
    period_key: Mapped[str] = mapped_column(String(128))
    status: Mapped[str] = mapped_column(String(32), default="pending")
    period_start: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    period_end: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    credits: Mapped[int] = mapped_column(Integer)
    amount_minor: Mapped[int] = mapped_column(Integer)
    currency: Mapped[str] = mapped_column(String(3))
    provider_invoice_id: Mapped[str | None] = mapped_column(String(255))
    provider_payment_id: Mapped[str | None] = mapped_column(String(255))
    payment_order_id: Mapped[UUID | None] = mapped_column(
        ForeignKey("payment_orders.id", ondelete="RESTRICT"),
        unique=True,
    )
    purchase_transaction_id: Mapped[UUID | None] = mapped_column(
        ForeignKey("credit_transactions.id", ondelete="RESTRICT"),
        unique=True,
    )
    idempotency_key: Mapped[str] = mapped_column(String(255), unique=True)
    paid_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        onupdate=func.now(),
    )
