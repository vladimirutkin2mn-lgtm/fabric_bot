"""allow subscription checkout reconciliation billing jobs

Revision ID: 20260805_13
Revises: 20260805_12
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "20260805_13"
down_revision: str | None = "20260805_12"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_OLD_TYPES = (
    "'webhook_processing','subscription_renewal','payment_reconciliation','refund_reconciliation'"
)
_NEW_TYPES = (
    "'webhook_processing','subscription_renewal','payment_reconciliation',"
    "'refund_reconciliation','subscription_checkout_reconcile'"
)


def _job_type_constraint_name() -> str:
    bind = op.get_bind()
    constraint_name = bind.scalar(
        sa.text(
            "SELECT constraint_row.conname "
            "FROM pg_constraint AS constraint_row "
            "JOIN pg_class AS table_row ON table_row.oid = constraint_row.conrelid "
            "JOIN pg_namespace AS schema_row ON schema_row.oid = table_row.relnamespace "
            "WHERE schema_row.nspname = current_schema() "
            "AND table_row.relname = 'billing_jobs' "
            "AND constraint_row.contype = 'c' "
            "AND pg_get_constraintdef(constraint_row.oid) LIKE '%job_type%' "
            "ORDER BY constraint_row.conname LIMIT 1"
        )
    )
    if not isinstance(constraint_name, str):
        raise RuntimeError("billing_jobs job_type check constraint was not found")
    return constraint_name


def _replace_constraint(values: str) -> None:
    bind = op.get_bind()
    quoted_name = bind.dialect.identifier_preparer.quote(_job_type_constraint_name())
    op.execute(sa.text(f"ALTER TABLE billing_jobs DROP CONSTRAINT {quoted_name}"))
    op.create_check_constraint(
        "ck_billing_jobs_type",
        "billing_jobs",
        f"job_type IN ({values})",
    )


def upgrade() -> None:
    _replace_constraint(_NEW_TYPES)


def downgrade() -> None:
    bind = op.get_bind()
    live_rows = bind.scalar(
        sa.text(
            "SELECT count(*) FROM billing_jobs WHERE job_type = 'subscription_checkout_reconcile'"
        )
    )
    if live_rows:
        raise RuntimeError("downgrade refused: subscription checkout reconciliation jobs exist")
    _replace_constraint(_OLD_TYPES)
