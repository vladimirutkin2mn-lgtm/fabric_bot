"""durable paid follow-up entitlement

Revision ID: 20260805_15
Revises: 20260805_14
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "20260805_15"
down_revision: str | None = "20260805_14"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "analysis_followups",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("analysis_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("user_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("status", sa.String(length=16), server_default="available", nullable=False),
        sa.Column("claim_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("lease_until", sa.DateTime(timezone=True), nullable=True),
        sa.Column("question_ciphertext", sa.LargeBinary(), nullable=True),
        sa.Column("answer_ciphertext", sa.LargeBinary(), nullable=True),
        sa.Column("prompt_version", sa.String(length=64), nullable=False),
        sa.Column("reservation_count", sa.Integer(), server_default="0", nullable=False),
        sa.Column("llm_attempt_count", sa.Integer(), server_default="0", nullable=False),
        sa.Column("llm_provider", sa.String(length=32), nullable=True),
        sa.Column("model_name", sa.String(length=255), nullable=True),
        sa.Column("provider_request_id", sa.String(length=255), nullable=True),
        sa.Column("input_tokens", sa.Integer(), nullable=True),
        sa.Column("output_tokens", sa.Integer(), nullable=True),
        sa.Column("latency_ms", sa.Integer(), nullable=True),
        sa.Column("last_failure_code", sa.String(length=64), nullable=True),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
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
            "status IN ('available','reserved','completed')",
            name="ck_analysis_followups_status",
        ),
        sa.CheckConstraint(
            "reservation_count >= 0 AND llm_attempt_count >= 0",
            name="ck_analysis_followups_attempts",
        ),
        sa.CheckConstraint(
            "(status = 'available' AND claim_id IS NULL AND lease_until IS NULL "
            "AND question_ciphertext IS NULL AND answer_ciphertext IS NULL "
            "AND completed_at IS NULL) OR "
            "(status = 'reserved' AND claim_id IS NOT NULL AND lease_until IS NOT NULL "
            "AND question_ciphertext IS NOT NULL AND answer_ciphertext IS NULL "
            "AND completed_at IS NULL) OR "
            "(status = 'completed' AND claim_id IS NULL AND lease_until IS NULL "
            "AND question_ciphertext IS NOT NULL AND answer_ciphertext IS NOT NULL "
            "AND completed_at IS NOT NULL)",
            name="ck_analysis_followups_state",
        ),
        sa.ForeignKeyConstraint(["analysis_id"], ["analyses.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("analysis_id", name="uq_analysis_followups_analysis"),
    )
    op.create_index(
        "ix_analysis_followups_user_id",
        "analysis_followups",
        ["user_id"],
        unique=False,
    )
    op.create_index(
        "ix_analysis_followups_expired_reservations",
        "analysis_followups",
        ["lease_until"],
        unique=False,
        postgresql_where=sa.text("status = 'reserved'"),
    )
    op.execute(
        sa.text(
            """
            CREATE OR REPLACE FUNCTION purge_analysis_followup_on_delete()
            RETURNS trigger AS $$
            BEGIN
              IF NEW.status = 'deleted' AND OLD.status IS DISTINCT FROM 'deleted' THEN
                DELETE FROM analysis_followups WHERE analysis_id = NEW.id;
              END IF;
              RETURN NEW;
            END;
            $$ LANGUAGE plpgsql
            """
        )
    )
    op.execute(
        sa.text(
            """
            CREATE TRIGGER trg_purge_analysis_followup_on_delete
            AFTER UPDATE OF status ON analyses
            FOR EACH ROW EXECUTE FUNCTION purge_analysis_followup_on_delete()
            """
        )
    )


def downgrade() -> None:
    bind = op.get_bind()
    live_rows = bind.scalar(sa.text("SELECT count(*) FROM analysis_followups"))
    if live_rows:
        raise RuntimeError("downgrade refused: analysis_followups contains entitlement state")
    op.execute(sa.text("DROP TRIGGER IF EXISTS trg_purge_analysis_followup_on_delete ON analyses"))
    op.execute(sa.text("DROP FUNCTION IF EXISTS purge_analysis_followup_on_delete()"))
    op.drop_index(
        "ix_analysis_followups_expired_reservations",
        table_name="analysis_followups",
    )
    op.drop_index("ix_analysis_followups_user_id", table_name="analysis_followups")
    op.drop_table("analysis_followups")
