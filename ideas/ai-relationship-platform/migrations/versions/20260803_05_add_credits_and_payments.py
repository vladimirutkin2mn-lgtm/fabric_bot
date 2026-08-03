# ruff: noqa: E501
"""Add credits, preview entitlement, payment orders and report access.

Revision ID: 20260803_05
Revises: 20260803_04
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "20260803_05"
down_revision: str | None = "20260803_04"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "users",
        sa.Column("free_preview_status", sa.String(16), server_default="available", nullable=False),
    )
    op.add_column("users", sa.Column("free_preview_analysis_id", postgresql.UUID(), nullable=True))
    op.add_column(
        "users", sa.Column("free_preview_used_at", sa.DateTime(timezone=True), nullable=True)
    )
    op.create_check_constraint(
        "ck_users_free_preview",
        "users",
        "(free_preview_status = 'available' AND free_preview_analysis_id IS NULL AND free_preview_used_at IS NULL) OR (free_preview_status = 'reserved' AND free_preview_analysis_id IS NOT NULL AND free_preview_used_at IS NULL) OR (free_preview_status = 'consumed' AND free_preview_used_at IS NOT NULL)",
    )
    op.add_column(
        "analyses", sa.Column("report_access", sa.String(16), server_default="none", nullable=False)
    )
    op.add_column(
        "analyses", sa.Column("cost_units", sa.Integer(), server_default="0", nullable=False)
    )
    op.add_column(
        "analyses", sa.Column("full_access_transaction_id", postgresql.UUID(), nullable=True)
    )
    op.execute("UPDATE analyses SET report_access = 'full' WHERE status = 'completed'")
    op.create_check_constraint(
        "ck_analyses_report_access", "analyses", "report_access IN ('none','preview','full')"
    )
    op.create_check_constraint("ck_analyses_cost_units", "analyses", "cost_units >= 0")
    op.create_check_constraint(
        "ck_analyses_access_state",
        "analyses",
        "(report_access = 'none') OR (report_access = 'preview' AND status = 'completed' AND cost_units = 0) OR (report_access = 'full' AND status = 'completed')",
    )
    op.create_check_constraint(
        "ck_analyses_paid_access_transaction",
        "analyses",
        "cost_units = 0 OR full_access_transaction_id IS NOT NULL",
    )
    op.create_check_constraint(
        "ck_analyses_deleted_access", "analyses", "status <> 'deleted' OR report_access = 'none'"
    )
    op.create_table(
        "payment_orders",
        sa.Column("id", postgresql.UUID(), primary_key=True),
        sa.Column(
            "user_id",
            postgresql.UUID(),
            sa.ForeignKey("users.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column("provider", sa.String(32), nullable=False),
        sa.Column("product_code", sa.String(64), nullable=False),
        sa.Column("status", sa.String(16), nullable=False),
        sa.Column("credits", sa.Integer(), nullable=False),
        sa.Column("amount_minor", sa.Integer(), nullable=False),
        sa.Column("currency", sa.String(3), nullable=False),
        sa.Column("checkout_token", postgresql.UUID(), unique=True, nullable=False),
        sa.Column("provider_checkout_id", sa.String(255), unique=True),
        sa.Column("provider_payment_id", sa.String(255), unique=True),
        sa.Column("provider_event_id", sa.String(255), unique=True),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column("completed_at", sa.DateTime(timezone=True)),
        sa.CheckConstraint(
            "status IN ('creating','pending','completed','failed','cancelled')",
            name="ck_payment_orders_status",
        ),
        sa.CheckConstraint("credits > 0 AND amount_minor > 0", name="ck_payment_orders_positive"),
        sa.CheckConstraint("char_length(currency) = 3", name="ck_payment_orders_currency"),
        sa.CheckConstraint(
            "(status = 'completed') = (completed_at IS NOT NULL AND provider_payment_id IS NOT NULL)",
            name="ck_payment_orders_completion",
        ),
    )
    op.create_index(
        "uq_payment_orders_active",
        "payment_orders",
        ["user_id", "provider", "product_code"],
        unique=True,
        postgresql_where=sa.text("status IN ('creating','pending')"),
    )
    op.create_table(
        "credit_transactions",
        sa.Column("id", postgresql.UUID(), primary_key=True),
        sa.Column(
            "user_id",
            postgresql.UUID(),
            sa.ForeignKey("users.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column("type", sa.String(16), nullable=False),
        sa.Column("amount", sa.Integer(), nullable=False),
        sa.Column("idempotency_key", sa.String(255), unique=True, nullable=False),
        sa.Column(
            "analysis_id", postgresql.UUID(), sa.ForeignKey("analyses.id", ondelete="RESTRICT")
        ),
        sa.Column(
            "payment_order_id",
            postgresql.UUID(),
            sa.ForeignKey("payment_orders.id", ondelete="RESTRICT"),
            unique=True,
        ),
        sa.Column(
            "reverses_transaction_id",
            postgresql.UUID(),
            sa.ForeignKey("credit_transactions.id", ondelete="RESTRICT"),
            unique=True,
        ),
        sa.Column("product_code", sa.String(64)),
        sa.Column("external_payment_id", sa.String(255), unique=True),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.CheckConstraint("amount <> 0", name="ck_credit_transactions_nonzero"),
        sa.CheckConstraint(
            "type IN ('grant','purchase','spend','refund','adjustment')",
            name="ck_credit_transactions_type",
        ),
        sa.CheckConstraint(
            "(type IN ('grant','purchase','refund') AND amount > 0) OR (type = 'spend' AND amount < 0) OR (type = 'adjustment' AND amount <> 0)",
            name="ck_credit_transactions_sign",
        ),
        sa.CheckConstraint(
            "type <> 'spend' OR analysis_id IS NOT NULL",
            name="ck_credit_transactions_spend_analysis",
        ),
        sa.CheckConstraint(
            "type <> 'purchase' OR (payment_order_id IS NOT NULL AND product_code IS NOT NULL)",
            name="ck_credit_transactions_purchase_order",
        ),
        sa.CheckConstraint(
            "type <> 'refund' OR reverses_transaction_id IS NOT NULL",
            name="ck_credit_transactions_refund_reversal",
        ),
    )
    op.create_index("ix_credit_transactions_user_id", "credit_transactions", ["user_id"])
    op.create_foreign_key(
        "fk_users_preview_analysis",
        "users",
        "analyses",
        ["free_preview_analysis_id"],
        ["id"],
        ondelete="SET NULL",
    )
    op.create_foreign_key(
        "fk_analyses_full_transaction",
        "analyses",
        "credit_transactions",
        ["full_access_transaction_id"],
        ["id"],
        ondelete="RESTRICT",
    )


def downgrade() -> None:
    op.drop_constraint("fk_analyses_full_transaction", "analyses", type_="foreignkey")
    op.drop_constraint("fk_users_preview_analysis", "users", type_="foreignkey")
    op.drop_table("credit_transactions")
    op.drop_index("uq_payment_orders_active", table_name="payment_orders")
    op.drop_table("payment_orders")
    op.drop_constraint("ck_analyses_deleted_access", "analyses", type_="check")
    op.drop_constraint("ck_analyses_paid_access_transaction", "analyses", type_="check")
    op.drop_constraint("ck_analyses_access_state", "analyses", type_="check")
    op.drop_constraint("ck_analyses_cost_units", "analyses", type_="check")
    op.drop_constraint("ck_analyses_report_access", "analyses", type_="check")
    op.drop_column("analyses", "full_access_transaction_id")
    op.drop_column("analyses", "cost_units")
    op.drop_column("analyses", "report_access")
    op.drop_constraint("ck_users_free_preview", "users", type_="check")
    op.drop_column("users", "free_preview_used_at")
    op.drop_column("users", "free_preview_analysis_id")
    op.drop_column("users", "free_preview_status")
