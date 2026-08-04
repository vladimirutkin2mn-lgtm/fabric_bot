"""durable encrypted Telegram updates

Revision ID: 20260804_10
Revises: 20260804_09
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "20260804_10"
down_revision: str | None = "20260804_09"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


_SCRUB_FUNCTION = """
CREATE FUNCTION scrub_telegram_updates_on_user_delete() RETURNS trigger AS $$
BEGIN
    IF OLD.telegram_user_id IS NOT NULL AND NEW.telegram_user_id IS NULL THEN
        UPDATE telegram_update_inbox
        SET payload_ciphertext = NULL,
            status = 'failed',
            last_error_code = 'user_deleted',
            claimed_by = NULL,
            claim_id = NULL,
            claimed_at = NULL,
            lease_until = NULL,
            completed_at = now(),
            updated_at = now()
        WHERE telegram_user_id = OLD.telegram_user_id
          AND status IN ('pending', 'claimed');
    END IF;
    RETURN NEW;
END;
$$ LANGUAGE plpgsql
"""


def upgrade() -> None:
    op.create_table(
        "telegram_update_inbox",
        sa.Column("update_id", sa.BigInteger(), nullable=False),
        sa.Column("telegram_user_id", sa.BigInteger(), nullable=True),
        sa.Column("payload_ciphertext", sa.LargeBinary(), nullable=True),
        sa.Column("payload_hash", sa.String(64), nullable=False),
        sa.Column("status", sa.String(16), server_default="pending", nullable=False),
        sa.Column("attempt_count", sa.Integer(), server_default="0", nullable=False),
        sa.Column(
            "available_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column("claimed_by", sa.String(255), nullable=True),
        sa.Column("claim_id", sa.Uuid(), nullable=True),
        sa.Column("claimed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("lease_until", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_error_code", sa.String(64), nullable=True),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint(
            "status IN ('pending','claimed','completed','failed')",
            name="ck_telegram_update_inbox_status",
        ),
        sa.CheckConstraint("attempt_count >= 0", name="ck_telegram_update_inbox_attempts"),
        sa.CheckConstraint(
            "(status IN ('pending','claimed') AND payload_ciphertext IS NOT NULL) OR "
            "(status IN ('completed','failed') AND payload_ciphertext IS NULL)",
            name="ck_telegram_update_inbox_payload_lifecycle",
        ),
        sa.PrimaryKeyConstraint("update_id"),
    )
    op.create_index(
        "ix_telegram_update_inbox_claimable",
        "telegram_update_inbox",
        ["status", "available_at"],
        unique=False,
    )
    op.create_index(
        "ix_telegram_update_inbox_user",
        "telegram_update_inbox",
        ["telegram_user_id"],
        unique=False,
    )
    op.execute(_SCRUB_FUNCTION)
    op.execute(
        "CREATE TRIGGER trg_scrub_telegram_updates_on_user_delete "
        "BEFORE UPDATE OF telegram_user_id ON users FOR EACH ROW "
        "EXECUTE FUNCTION scrub_telegram_updates_on_user_delete()"
    )


def downgrade() -> None:
    op.execute("DROP TRIGGER IF EXISTS trg_scrub_telegram_updates_on_user_delete ON users")
    op.execute("DROP FUNCTION IF EXISTS scrub_telegram_updates_on_user_delete()")
    op.drop_index("ix_telegram_update_inbox_user", table_name="telegram_update_inbox")
    op.drop_index("ix_telegram_update_inbox_claimable", table_name="telegram_update_inbox")
    op.drop_table("telegram_update_inbox")
