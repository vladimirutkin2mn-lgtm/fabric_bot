# ruff: noqa: E501
"""Production billing foundation.

Revision ID: 20260803_06
Revises: 20260803_05
"""

from collections.abc import Sequence

from alembic import op

revision: str = "20260803_06"
down_revision: str | None = "20260803_05"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def _execute_script(script: str) -> None:
    for statement in script.split(";"):
        if statement.strip():
            op.execute(statement)


def upgrade() -> None:
    _execute_script("""
    ALTER TABLE payment_orders ADD COLUMN mode varchar(32) NOT NULL DEFAULT 'one_time', ADD COLUMN market varchar(32) NOT NULL DEFAULT 'RU', ADD COLUMN product_version integer NOT NULL DEFAULT 1, ADD COLUMN billing_period varchar(32), ADD COLUMN provider_invoice_id varchar(255), ADD COLUMN subscription_id uuid, ADD COLUMN provider_status varchar(64), ADD COLUMN idempotency_key varchar(255), ADD COLUMN provider_request_id varchar(255), ADD COLUMN failure_code varchar(64), ADD COLUMN commercial_snapshot jsonb NOT NULL DEFAULT '{}'::jsonb;
    CREATE UNIQUE INDEX uq_payment_orders_idempotency_key ON payment_orders(idempotency_key);
    CREATE TABLE billing_customers (id uuid PRIMARY KEY, user_id uuid NOT NULL REFERENCES users(id) ON DELETE RESTRICT, provider varchar(32) NOT NULL, provider_customer_id varchar(255) NOT NULL, created_at timestamptz NOT NULL DEFAULT now(), updated_at timestamptz NOT NULL DEFAULT now(), UNIQUE(user_id,provider), UNIQUE(provider,provider_customer_id)); CREATE INDEX ix_billing_customers_user_id ON billing_customers(user_id);
    CREATE TABLE subscriptions (id uuid PRIMARY KEY, user_id uuid NOT NULL REFERENCES users(id) ON DELETE RESTRICT, billing_customer_id uuid NOT NULL REFERENCES billing_customers(id) ON DELETE RESTRICT, provider varchar(32) NOT NULL, provider_subscription_id varchar(255) NOT NULL UNIQUE, product_code varchar(64) NOT NULL, product_version integer NOT NULL, status varchar(32) NOT NULL DEFAULT 'incomplete' CHECK (status IN ('incomplete','active','past_due','cancel_at_period_end','canceled','unpaid','paused')), encrypted_payment_method bytea, current_period_start timestamptz, current_period_end timestamptz, cancel_at timestamptz, canceled_at timestamptz, consent_version varchar(64) NOT NULL, consented_at timestamptz NOT NULL, renewal_claimed_by varchar(255), renewal_lease_until timestamptz, last_order_id uuid, created_at timestamptz NOT NULL DEFAULT now(), updated_at timestamptz NOT NULL DEFAULT now()); CREATE INDEX ix_subscriptions_user_id ON subscriptions(user_id); CREATE UNIQUE INDEX uq_subscriptions_active_user_product ON subscriptions(user_id,product_code) WHERE status IN ('incomplete','active','past_due','cancel_at_period_end','paused');
    ALTER TABLE payment_orders ADD CONSTRAINT fk_payment_orders_subscription FOREIGN KEY(subscription_id) REFERENCES subscriptions(id) ON DELETE RESTRICT; ALTER TABLE subscriptions ADD CONSTRAINT fk_subscriptions_last_order FOREIGN KEY(last_order_id) REFERENCES payment_orders(id) ON DELETE RESTRICT;
    CREATE TABLE provider_webhook_events (id uuid PRIMARY KEY, provider varchar(32) NOT NULL, provider_event_id varchar(255) NOT NULL, event_type varchar(128) NOT NULL, provider_object_id varchar(255) NOT NULL, payload_hash varchar(64) NOT NULL, status varchar(32) NOT NULL DEFAULT 'pending' CHECK(status IN ('pending','processing','completed','failed','manual_review')), attempt_count integer NOT NULL DEFAULT 0, received_at timestamptz NOT NULL DEFAULT now(), processed_at timestamptz, last_error_code varchar(64), updated_at timestamptz NOT NULL DEFAULT now(), UNIQUE(provider,provider_event_id));
    CREATE TABLE refund_requests (id uuid PRIMARY KEY, user_id uuid NOT NULL REFERENCES users(id) ON DELETE RESTRICT, payment_order_id uuid NOT NULL REFERENCES payment_orders(id) ON DELETE RESTRICT, provider varchar(32) NOT NULL, provider_refund_id varchar(255) UNIQUE, status varchar(32) NOT NULL DEFAULT 'requested' CHECK(status IN ('requested','credits_reserved','provider_pending','succeeded','failed','manual_review')), amount_minor integer NOT NULL, currency varchar(3) NOT NULL, credit_units integer NOT NULL, reason varchar(255) NOT NULL, idempotency_key varchar(255) NOT NULL UNIQUE, provider_request_id varchar(255), failure_code varchar(64), created_at timestamptz NOT NULL DEFAULT now(), updated_at timestamptz NOT NULL DEFAULT now(), CHECK(amount_minor > 0 AND credit_units > 0)); CREATE INDEX ix_refund_requests_user_id ON refund_requests(user_id);
    CREATE TABLE credit_reservations (id uuid PRIMARY KEY, user_id uuid NOT NULL REFERENCES users(id) ON DELETE RESTRICT, refund_request_id uuid NOT NULL UNIQUE REFERENCES refund_requests(id) ON DELETE RESTRICT, credit_units integer NOT NULL CHECK(credit_units > 0), status varchar(16) NOT NULL DEFAULT 'active' CHECK(status IN ('active','consumed','released')), created_at timestamptz NOT NULL DEFAULT now(), updated_at timestamptz NOT NULL DEFAULT now()); CREATE INDEX ix_credit_reservations_user_id ON credit_reservations(user_id);
    CREATE TABLE billing_jobs (id uuid PRIMARY KEY, job_type varchar(32) NOT NULL CHECK(job_type IN ('webhook_processing','subscription_renewal','payment_reconciliation','refund_reconciliation')), provider varchar(32) NOT NULL, object_type varchar(64) NOT NULL, object_id varchar(255) NOT NULL, idempotency_key varchar(255) NOT NULL UNIQUE, status varchar(32) NOT NULL DEFAULT 'pending' CHECK(status IN ('pending','claimed','completed','failed','manual_review')), attempt_count integer NOT NULL DEFAULT 0, available_at timestamptz NOT NULL DEFAULT now(), claimed_by varchar(255), claimed_at timestamptz, lease_until timestamptz, last_error_code varchar(64), created_at timestamptz NOT NULL DEFAULT now(), updated_at timestamptz NOT NULL DEFAULT now());
    ALTER TABLE credit_transactions DROP CONSTRAINT ck_credit_transactions_type, DROP CONSTRAINT ck_credit_transactions_sign; ALTER TABLE credit_transactions ADD COLUMN original_purchase_transaction_id uuid REFERENCES credit_transactions(id) ON DELETE RESTRICT, ADD COLUMN refund_request_id uuid UNIQUE, ADD CONSTRAINT ck_credit_transactions_type CHECK(type IN ('grant','purchase','spend','refund','adjustment','purchase_refund')), ADD CONSTRAINT ck_credit_transactions_sign CHECK((type IN ('grant','purchase','refund') AND amount > 0) OR (type='purchase_refund' AND amount < 0) OR (type='spend' AND amount < 0) OR (type='adjustment' AND amount <> 0)), ADD CONSTRAINT fk_credit_transactions_refund_request FOREIGN KEY(refund_request_id) REFERENCES refund_requests(id) ON DELETE RESTRICT;
    """)


def downgrade() -> None:
    _execute_script("""
    ALTER TABLE credit_transactions DROP CONSTRAINT fk_credit_transactions_refund_request, DROP COLUMN refund_request_id, DROP COLUMN original_purchase_transaction_id, DROP CONSTRAINT ck_credit_transactions_type, DROP CONSTRAINT ck_credit_transactions_sign, ADD CONSTRAINT ck_credit_transactions_type CHECK(type IN ('grant','purchase','spend','refund','adjustment')), ADD CONSTRAINT ck_credit_transactions_sign CHECK((type IN ('grant','purchase','refund') AND amount > 0) OR (type='spend' AND amount < 0) OR (type='adjustment' AND amount <> 0));
    DROP TABLE billing_jobs; DROP TABLE credit_reservations; DROP TABLE refund_requests; DROP TABLE provider_webhook_events; ALTER TABLE subscriptions DROP CONSTRAINT fk_subscriptions_last_order; ALTER TABLE payment_orders DROP CONSTRAINT fk_payment_orders_subscription; DROP TABLE subscriptions; DROP TABLE billing_customers; DROP INDEX uq_payment_orders_idempotency_key; ALTER TABLE payment_orders DROP COLUMN commercial_snapshot, DROP COLUMN failure_code, DROP COLUMN provider_request_id, DROP COLUMN idempotency_key, DROP COLUMN provider_status, DROP COLUMN subscription_id, DROP COLUMN provider_invoice_id, DROP COLUMN billing_period, DROP COLUMN product_version, DROP COLUMN market, DROP COLUMN mode;
    """)
