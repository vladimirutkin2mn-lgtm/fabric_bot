# ruff: noqa: E501
"""Production one-time payment processing.

Revision ID: 20260803_07
Revises: 20260803_06
"""

from collections.abc import Sequence

from alembic import op

revision: str = "20260803_07"
down_revision: str | None = "20260803_06"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute("DROP INDEX uq_payment_orders_active")
    op.execute(
        "CREATE UNIQUE INDEX uq_payment_orders_active ON payment_orders(user_id, provider, product_code, market, currency) WHERE status IN ('creating','pending')"
    )
    op.execute("ALTER TABLE payment_orders DROP CONSTRAINT ck_payment_orders_status")
    op.execute(
        "ALTER TABLE payment_orders ADD CONSTRAINT ck_payment_orders_status CHECK(status IN ('creating','pending','completed','failed','cancelled','manual_review'))"
    )
    op.execute(
        "ALTER TABLE payment_orders ADD COLUMN checkout_expires_at timestamptz, ADD COLUMN last_reconciled_at timestamptz, ADD COLUMN provider_live_mode boolean, ADD COLUMN encrypted_receipt_contact bytea"
    )
    op.execute(
        "CREATE INDEX ix_payment_orders_reconciliation ON payment_orders(status, updated_at) WHERE status IN ('creating','pending')"
    )
    op.execute(
        "CREATE INDEX ix_billing_jobs_claim ON billing_jobs(status, available_at, lease_until)"
    )
    op.execute(
        "CREATE TABLE billing_outbox_events (id uuid PRIMARY KEY, aggregate_type varchar(64) NOT NULL, aggregate_id varchar(255) NOT NULL, event_type varchar(64) NOT NULL, payload jsonb NOT NULL DEFAULT '{}'::jsonb, idempotency_key varchar(255) NOT NULL UNIQUE, status varchar(32) NOT NULL DEFAULT 'pending' CHECK(status IN ('pending','claimed','completed','failed','manual_review')), attempt_count integer NOT NULL DEFAULT 0, available_at timestamptz NOT NULL DEFAULT now(), claimed_by varchar(255), claimed_at timestamptz, lease_until timestamptz, last_error_code varchar(64), created_at timestamptz NOT NULL DEFAULT now(), completed_at timestamptz)"
    )
    op.execute(
        "CREATE INDEX ix_billing_outbox_claim ON billing_outbox_events(status, available_at, lease_until)"
    )


def downgrade() -> None:
    op.execute("DROP TABLE billing_outbox_events")
    op.execute("DROP INDEX ix_billing_jobs_claim")
    op.execute("DROP INDEX ix_payment_orders_reconciliation")
    op.execute("DROP INDEX uq_payment_orders_active")
    op.execute(
        "CREATE UNIQUE INDEX uq_payment_orders_active ON payment_orders(user_id, provider, product_code) WHERE status IN ('creating','pending')"
    )
    op.execute(
        "ALTER TABLE payment_orders DROP COLUMN encrypted_receipt_contact, DROP COLUMN provider_live_mode, DROP COLUMN last_reconciled_at, DROP COLUMN checkout_expires_at"
    )
    op.execute("ALTER TABLE payment_orders DROP CONSTRAINT ck_payment_orders_status")
    op.execute(
        "ALTER TABLE payment_orders ADD CONSTRAINT ck_payment_orders_status CHECK(status IN ('creating','pending','completed','failed','cancelled'))"
    )
