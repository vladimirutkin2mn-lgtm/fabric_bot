"""durable subscription billing periods

Revision ID: 20260805_12
Revises: 20260804_11
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "20260805_12"
down_revision: str | None = "20260804_11"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "subscription_periods",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("subscription_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("provider", sa.String(length=32), nullable=False),
        sa.Column("period_key", sa.String(length=128), nullable=False),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("period_start", sa.DateTime(timezone=True), nullable=False),
        sa.Column("period_end", sa.DateTime(timezone=True), nullable=False),
        sa.Column("credits", sa.Integer(), nullable=False),
        sa.Column("amount_minor", sa.Integer(), nullable=False),
        sa.Column("currency", sa.String(length=3), nullable=False),
        sa.Column("provider_invoice_id", sa.String(length=255), nullable=True),
        sa.Column("provider_payment_id", sa.String(length=255), nullable=True),
        sa.Column("payment_order_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("purchase_transaction_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("idempotency_key", sa.String(length=255), nullable=False),
        sa.Column("paid_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.CheckConstraint(
            "status IN ('pending','paid','past_due','failed','canceled','manual_review')",
            name="ck_subscription_periods_status",
        ),
        sa.CheckConstraint(
            "credits > 0 AND amount_minor > 0",
            name="ck_subscription_periods_positive",
        ),
        sa.CheckConstraint(
            "char_length(currency) = 3",
            name="ck_subscription_periods_currency",
        ),
        sa.CheckConstraint(
            "period_end > period_start",
            name="ck_subscription_periods_range",
        ),
        sa.CheckConstraint(
            "(status = 'paid') = "
            "(paid_at IS NOT NULL AND payment_order_id IS NOT NULL "
            "AND purchase_transaction_id IS NOT NULL AND provider_payment_id IS NOT NULL)",
            name="ck_subscription_periods_paid_refs",
        ),
        sa.ForeignKeyConstraint(
            ["subscription_id"],
            ["subscriptions.id"],
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["payment_order_id"],
            ["payment_orders.id"],
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["purchase_transaction_id"],
            ["credit_transactions.id"],
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "subscription_id",
            "period_key",
            name="uq_subscription_period_key",
        ),
        sa.UniqueConstraint(
            "provider",
            "provider_invoice_id",
            name="uq_subscription_period_provider_invoice",
        ),
        sa.UniqueConstraint(
            "provider",
            "provider_payment_id",
            name="uq_subscription_period_provider_payment",
        ),
        sa.UniqueConstraint("payment_order_id"),
        sa.UniqueConstraint("purchase_transaction_id"),
        sa.UniqueConstraint("idempotency_key"),
    )
    op.create_index(
        "ix_subscription_periods_subscription_id",
        "subscription_periods",
        ["subscription_id"],
        unique=False,
    )
    op.create_index(
        "ix_subscription_periods_subscription_end",
        "subscription_periods",
        ["subscription_id", "period_end"],
        unique=False,
    )
    op.create_index(
        "ix_subscription_periods_status_end",
        "subscription_periods",
        ["status", "period_end"],
        unique=False,
    )


def downgrade() -> None:
    bind = op.get_bind()
    live_rows = bind.scalar(sa.text("SELECT count(*) FROM subscription_periods"))
    if live_rows:
        raise RuntimeError("downgrade refused: subscription_periods contains financial state")
    op.drop_index("ix_subscription_periods_status_end", table_name="subscription_periods")
    op.drop_index("ix_subscription_periods_subscription_end", table_name="subscription_periods")
    op.drop_index("ix_subscription_periods_subscription_id", table_name="subscription_periods")
    op.drop_table("subscription_periods")
