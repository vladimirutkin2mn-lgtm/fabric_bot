"""allow purchase refund ledger entries

Revision ID: 20260805_14
Revises: 20260805_13
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "20260805_14"
down_revision: str | None = "20260805_13"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def _payment_order_unique_constraint() -> str:
    bind = op.get_bind()
    name = bind.scalar(
        sa.text(
            "SELECT constraint_row.conname "
            "FROM pg_constraint AS constraint_row "
            "JOIN pg_class AS table_row ON table_row.oid = constraint_row.conrelid "
            "JOIN pg_namespace AS schema_row ON schema_row.oid = table_row.relnamespace "
            "WHERE schema_row.nspname = current_schema() "
            "AND table_row.relname = 'credit_transactions' "
            "AND constraint_row.contype = 'u' "
            "AND pg_get_constraintdef(constraint_row.oid) = 'UNIQUE (payment_order_id)' "
            "ORDER BY constraint_row.conname LIMIT 1"
        )
    )
    if not isinstance(name, str):
        raise RuntimeError("credit_transactions payment_order_id unique constraint was not found")
    return name


def upgrade() -> None:
    bind = op.get_bind()
    quoted = bind.dialect.identifier_preparer.quote(_payment_order_unique_constraint())
    op.execute(sa.text(f"ALTER TABLE credit_transactions DROP CONSTRAINT {quoted}"))
    op.create_index(
        "uq_credit_transactions_purchase_order",
        "credit_transactions",
        ["payment_order_id"],
        unique=True,
        postgresql_where=sa.text("type = 'purchase'"),
    )
    op.create_index(
        "ix_refund_requests_payment_order_id",
        "refund_requests",
        ["payment_order_id"],
        unique=False,
    )


def downgrade() -> None:
    bind = op.get_bind()
    live_rows = bind.scalar(
        sa.text("SELECT count(*) FROM credit_transactions WHERE type = 'purchase_refund'")
    )
    if live_rows:
        raise RuntimeError("downgrade refused: purchase refund ledger entries exist")
    op.drop_index("ix_refund_requests_payment_order_id", table_name="refund_requests")
    op.drop_index("uq_credit_transactions_purchase_order", table_name="credit_transactions")
    op.create_unique_constraint(
        "credit_transactions_payment_order_id_key",
        "credit_transactions",
        ["payment_order_id"],
    )
