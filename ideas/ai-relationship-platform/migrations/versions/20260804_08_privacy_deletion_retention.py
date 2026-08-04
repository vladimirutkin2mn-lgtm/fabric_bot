"""privacy deletion retention

Revision ID: 20260804_08
Revises: 20260803_07
"""
# ruff: noqa: E501

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "20260804_08"
down_revision: str | None = "20260803_07"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "analysis_private_content",
        sa.Column("analysis_id", sa.Uuid(), nullable=False),
        sa.Column("source_ciphertext", sa.LargeBinary(), nullable=True),
        sa.Column("result_ciphertext", sa.LargeBinary(), nullable=True),
        sa.Column("source_format_version", sa.Integer(), nullable=True),
        sa.Column("result_format_version", sa.Integer(), nullable=True),
        sa.Column("source_delete_after", sa.DateTime(timezone=True), nullable=True),
        sa.Column("source_deleted_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("result_deleted_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.ForeignKeyConstraint(["analysis_id"], ["analyses.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("analysis_id"),
    )
    op.create_index(
        "ix_private_source_delete_after", "analysis_private_content", ["source_delete_after"]
    )
    op.add_column(
        "users", sa.Column("privacy_status", sa.String(16), server_default="active", nullable=False)
    )
    op.add_column("users", sa.Column("deleted_at", sa.DateTime(timezone=True), nullable=True))
    op.alter_column("users", "telegram_user_id", existing_type=sa.BigInteger(), nullable=True)
    op.alter_column("users", "first_name", existing_type=sa.String(255), nullable=True)
    op.alter_column(
        "billing_customers", "provider_customer_id", existing_type=sa.String(255), nullable=True
    )
    op.create_check_constraint(
        "ck_users_privacy_identity",
        "users",
        "(privacy_status = 'active' AND telegram_user_id IS NOT NULL AND first_name IS NOT NULL AND deleted_at IS NULL) OR "
        "(privacy_status = 'deleted' AND telegram_user_id IS NULL AND telegram_username IS NULL AND first_name IS NULL AND telegram_language IS NULL AND deleted_at IS NOT NULL)",
    )
    op.add_column("analyses", sa.Column("deleted_at", sa.DateTime(timezone=True), nullable=True))
    op.create_index("ix_analyses_deleted_at", "analyses", ["deleted_at"])
    op.drop_constraint("ck_analyses_terminal_result", "analyses", type_="check")
    op.create_check_constraint(
        "ck_analyses_terminal_result",
        "analyses",
        "(status <> 'completed' OR completed_at IS NOT NULL) AND "
        "(status <> 'failed' OR (result_json IS NULL AND failure_code IS NOT NULL AND completed_at IS NULL))",
    )


def downgrade() -> None:
    bind = op.get_bind()
    incompatible = bind.scalar(
        sa.text(
            "SELECT EXISTS (SELECT 1 FROM users WHERE privacy_status = 'deleted') OR "
            "EXISTS (SELECT 1 FROM analyses a JOIN analysis_private_content p "
            "ON p.analysis_id = a.id WHERE a.status = 'completed' "
            "AND a.result_json IS NULL AND p.result_ciphertext IS NOT NULL)"
        )
    )
    if incompatible:
        raise RuntimeError(
            "privacy migration downgrade refused: tombstones or encrypted-only reports exist; "
            "restore compatible identity/result data through an audited operation first"
        )
    op.drop_constraint("ck_analyses_terminal_result", "analyses", type_="check")
    op.create_check_constraint(
        "ck_analyses_terminal_result",
        "analyses",
        "(status <> 'completed' OR (result_json IS NOT NULL AND completed_at IS NOT NULL)) AND "
        "(status <> 'failed' OR (result_json IS NULL AND failure_code IS NOT NULL AND completed_at IS NULL))",
    )
    op.drop_index("ix_analyses_deleted_at", table_name="analyses")
    op.drop_column("analyses", "deleted_at")
    op.drop_constraint("ck_users_privacy_identity", "users", type_="check")
    op.alter_column("users", "first_name", existing_type=sa.String(255), nullable=False)
    op.alter_column(
        "billing_customers", "provider_customer_id", existing_type=sa.String(255), nullable=False
    )
    op.alter_column("users", "telegram_user_id", existing_type=sa.BigInteger(), nullable=False)
    op.drop_column("users", "deleted_at")
    op.drop_column("users", "privacy_status")
    op.drop_index("ix_private_source_delete_after", table_name="analysis_private_content")
    op.drop_table("analysis_private_content")
