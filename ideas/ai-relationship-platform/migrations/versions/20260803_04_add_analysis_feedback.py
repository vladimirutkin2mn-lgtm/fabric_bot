"""Add atomic report feedback fields.

Revision ID: 20260803_04
Revises: 20260803_03
"""
# ruff: noqa: E501

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "20260803_04"
down_revision: str | None = "20260803_03"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("analyses", sa.Column("feedback_score", sa.Integer(), nullable=True))
    op.add_column(
        "analyses", sa.Column("feedback_submitted_at", sa.DateTime(timezone=True), nullable=True)
    )
    op.create_check_constraint(
        "ck_analyses_feedback",
        "analyses",
        "feedback_score IS NULL OR (feedback_score BETWEEN 1 AND 5 AND feedback_submitted_at IS NOT NULL)",
    )
    op.create_check_constraint(
        "ck_analyses_deleted_feedback",
        "analyses",
        "status <> 'deleted' OR (feedback_score IS NULL AND feedback_submitted_at IS NULL)",
    )


def downgrade() -> None:
    op.drop_constraint("ck_analyses_deleted_feedback", "analyses", type_="check")
    op.drop_constraint("ck_analyses_feedback", "analyses", type_="check")
    op.drop_column("analyses", "feedback_submitted_at")
    op.drop_column("analyses", "feedback_score")
