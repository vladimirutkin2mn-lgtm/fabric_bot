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
    "'webhook_processing','subscription_renewal','payment_reconciliation',"
    "'refund_reconciliation'"
)
_NEW_TYPES = _OLD_TYPES[:-1] + ",'subscription_checkout_reconcile'"


def _replace_constraint(values: str) -> None:
    op.drop_constraint("ck_billing_jobs_type", "billing_jobs", type_="check")
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
            "SELECT count(*) FROM billing_jobs "
            "WHERE job_type = 'subscription_checkout_reconcile'"
        )
    )
    if live_rows:
        raise RuntimeError(
            "downgrade refused: subscription checkout reconciliation jobs exist"
        )
    _replace_constraint(_OLD_TYPES)
