"""append-only release acceptance gates

Revision ID: 20260805_16
Revises: 20260805_15
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "20260805_16"
down_revision: str | None = "20260805_15"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "release_gate_attestations",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("gate_name", sa.String(length=64), nullable=False),
        sa.Column("status", sa.String(length=16), nullable=False),
        sa.Column("checklist_version", sa.String(length=64), nullable=False),
        sa.Column("app_env", sa.String(length=16), nullable=False),
        sa.Column("code_sha", sa.String(length=64), nullable=False),
        sa.Column("schema_revision", sa.String(length=64), nullable=False),
        sa.Column("evidence_ref", sa.String(length=512), nullable=False),
        sa.Column(
            "attested_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.CheckConstraint(
            "status IN ('passed','failed')",
            name="ck_release_gate_status",
        ),
        sa.CheckConstraint(
            "app_env = 'staging'",
            name="ck_release_gate_environment",
        ),
        sa.CheckConstraint(
            "char_length(evidence_ref) BETWEEN 1 AND 512",
            name="ck_release_gate_evidence",
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_release_gate_latest",
        "release_gate_attestations",
        ["gate_name", "attested_at"],
        unique=False,
    )
    op.execute(
        sa.text(
            """
            CREATE OR REPLACE FUNCTION prevent_release_gate_mutation()
            RETURNS trigger AS $$
            BEGIN
              RAISE EXCEPTION 'release gate attestations are append-only';
            END;
            $$ LANGUAGE plpgsql
            """
        )
    )
    op.execute(
        sa.text(
            """
            CREATE TRIGGER trg_prevent_release_gate_mutation
            BEFORE UPDATE OR DELETE ON release_gate_attestations
            FOR EACH ROW EXECUTE FUNCTION prevent_release_gate_mutation()
            """
        )
    )


def downgrade() -> None:
    bind = op.get_bind()
    live_rows = bind.scalar(sa.text("SELECT count(*) FROM release_gate_attestations"))
    if live_rows:
        raise RuntimeError("downgrade refused: release gate attestations contain audit history")
    op.execute(
        sa.text(
            "DROP TRIGGER IF EXISTS trg_prevent_release_gate_mutation ON release_gate_attestations"
        )
    )
    op.execute(sa.text("DROP FUNCTION IF EXISTS prevent_release_gate_mutation()"))
    op.drop_index("ix_release_gate_latest", table_name="release_gate_attestations")
    op.drop_table("release_gate_attestations")
