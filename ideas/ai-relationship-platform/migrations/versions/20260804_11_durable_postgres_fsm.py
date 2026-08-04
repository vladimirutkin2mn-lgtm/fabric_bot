"""durable encrypted aiogram FSM

Revision ID: 20260804_11
Revises: 20260804_10
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "20260804_11"
down_revision: str | None = "20260804_10"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


_DELETE_FUNCTION = """
CREATE FUNCTION delete_telegram_fsm_on_user_delete() RETURNS trigger AS $$
BEGIN
    IF OLD.telegram_user_id IS NOT NULL AND NEW.telegram_user_id IS NULL THEN
        DELETE FROM telegram_fsm_state
        WHERE user_id = OLD.telegram_user_id
           OR chat_id = OLD.telegram_user_id;
    END IF;
    RETURN NEW;
END;
$$ LANGUAGE plpgsql
"""


def upgrade() -> None:
    op.create_table(
        "telegram_fsm_state",
        sa.Column("bot_id", sa.BigInteger(), nullable=False),
        sa.Column("chat_id", sa.BigInteger(), nullable=False),
        sa.Column("user_id", sa.BigInteger(), nullable=False),
        sa.Column("thread_id", sa.BigInteger(), server_default="0", nullable=False),
        sa.Column(
            "business_connection_id", sa.String(255), server_default="", nullable=False
        ),
        sa.Column("destiny", sa.String(255), server_default="default", nullable=False),
        sa.Column("state", sa.String(255), nullable=True),
        sa.Column("data_ciphertext", sa.LargeBinary(), nullable=True),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.PrimaryKeyConstraint(
            "bot_id",
            "chat_id",
            "user_id",
            "thread_id",
            "business_connection_id",
            "destiny",
        ),
    )
    op.create_index(
        "ix_telegram_fsm_state_user",
        "telegram_fsm_state",
        ["user_id"],
        unique=False,
    )
    op.create_index(
        "ix_telegram_update_inbox_user_order",
        "telegram_update_inbox",
        ["telegram_user_id", "update_id", "status"],
        unique=False,
    )
    op.execute(_DELETE_FUNCTION)
    op.execute(
        "CREATE TRIGGER trg_delete_telegram_fsm_on_user_delete "
        "BEFORE UPDATE OF telegram_user_id ON users FOR EACH ROW "
        "EXECUTE FUNCTION delete_telegram_fsm_on_user_delete()"
    )


def downgrade() -> None:
    bind = op.get_bind()
    live_rows = bind.scalar(sa.text("SELECT count(*) FROM telegram_fsm_state"))
    if live_rows:
        raise RuntimeError("downgrade refused: telegram_fsm_state contains live state")
    op.execute("DROP TRIGGER IF EXISTS trg_delete_telegram_fsm_on_user_delete ON users")
    op.execute("DROP FUNCTION IF EXISTS delete_telegram_fsm_on_user_delete()")
    op.drop_index("ix_telegram_update_inbox_user_order", table_name="telegram_update_inbox")
    op.drop_index("ix_telegram_fsm_state_user", table_name="telegram_fsm_state")
    op.drop_table("telegram_fsm_state")
