"""Create durable analysis intake drafts.

Revision ID: 20260802_02
Revises: 20260801_01
"""
# ruff: noqa: E501

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "20260802_02"
down_revision: str | None = "20260801_01"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "analyses",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column(
            "user_id", sa.Uuid(), sa.ForeignKey("users.id", ondelete="CASCADE"), nullable=False
        ),
        sa.Column("status", sa.String(16), nullable=False, server_default="draft"),
        sa.Column(
            "intake_step", sa.String(40), nullable=False, server_default="waiting_for_conversation"
        ),
        sa.Column("source_type", sa.String(20), nullable=False, server_default="text"),
        sa.Column("normalized_conversation_json", postgresql.JSONB(), nullable=True),
        sa.Column("participants_json", postgresql.JSONB(), nullable=True),
        sa.Column("user_participant_label", sa.String(8)),
        sa.Column("user_goal", sa.Text()),
        sa.Column("relationship_stage", sa.String(32)),
        sa.Column("message_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("character_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.CheckConstraint(
            "status IN ('draft','queued','processing','completed','failed','deleted')",
            name="ck_analyses_status",
        ),
        sa.CheckConstraint(
            "intake_step IN ('waiting_for_conversation','waiting_for_participant','waiting_for_goal','waiting_for_relationship_stage','complete')",
            name="ck_analyses_intake_step",
        ),
        sa.CheckConstraint(
            "message_count >= 0 AND character_count >= 0", name="ck_analyses_counts"
        ),
    )
    op.create_index("ix_analyses_user_id", "analyses", ["user_id"])
    op.create_index(
        "uq_analyses_active_draft_user",
        "analyses",
        ["user_id"],
        unique=True,
        postgresql_where=sa.text("status = 'draft' AND intake_step <> 'complete'"),
    )


def downgrade() -> None:
    op.drop_table("analyses")
