"""analytics and admin observability

Revision ID: 20260804_09
Revises: 20260804_08
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "20260804_09"
down_revision: str | None = "20260804_08"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


_PROJECT_FUNCTION = """
CREATE FUNCTION project_billing_outbox_to_analytics() RETURNS trigger AS $$
DECLARE
    safe_properties jsonb;
BEGIN
    IF NEW.event_type NOT IN ('checkout_started', 'purchase_completed', 'payment_failed') THEN
        RETURN NEW;
    END IF;
    safe_properties := jsonb_strip_nulls(jsonb_build_object(
        'order_id', NEW.aggregate_id,
        'product_code', NEW.payload ->> 'product_code',
        'provider', NEW.payload ->> 'provider',
        'market', NEW.payload ->> 'market',
        'currency', NEW.payload ->> 'currency',
        'credits', NEW.payload ->> 'credits',
        'failure_code', NEW.payload ->> 'failure_code'
    ));
    INSERT INTO analytics_events (
        id,
        event_name,
        subject_id,
        properties,
        idempotency_key,
        correlation_id
    ) VALUES (
        md5(NEW.id::text)::uuid,
        NEW.event_type,
        NULL,
        safe_properties,
        'billing_outbox:' || NEW.idempotency_key,
        NULL
    )
    ON CONFLICT (idempotency_key) DO NOTHING;
    RETURN NEW;
END;
$$ LANGUAGE plpgsql
"""


_BACKFILL = """
INSERT INTO analytics_events (
    id,
    event_name,
    subject_id,
    properties,
    idempotency_key,
    correlation_id,
    created_at
)
SELECT
    md5(id::text)::uuid,
    event_type,
    NULL,
    jsonb_strip_nulls(jsonb_build_object(
        'order_id', aggregate_id,
        'product_code', payload ->> 'product_code',
        'provider', payload ->> 'provider',
        'market', payload ->> 'market',
        'currency', payload ->> 'currency',
        'credits', payload ->> 'credits',
        'failure_code', payload ->> 'failure_code'
    )),
    'billing_outbox:' || idempotency_key,
    NULL,
    created_at
FROM billing_outbox_events
WHERE event_type IN ('checkout_started', 'purchase_completed', 'payment_failed')
ON CONFLICT (idempotency_key) DO NOTHING
"""


def upgrade() -> None:
    op.create_table(
        "analytics_events",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("event_name", sa.String(64), nullable=False),
        sa.Column("subject_id", sa.String(64), nullable=True),
        sa.Column(
            "properties",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'{}'::jsonb"),
            nullable=False,
        ),
        sa.Column("idempotency_key", sa.String(255), nullable=False),
        sa.Column("correlation_id", sa.String(64), nullable=True),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.CheckConstraint(
            "jsonb_typeof(properties) = 'object'", name="ck_analytics_events_properties_object"
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("idempotency_key", name="uq_analytics_events_idempotency_key"),
    )
    op.create_index(
        "ix_analytics_events_name_created",
        "analytics_events",
        ["event_name", "created_at"],
        unique=False,
    )
    op.create_index(
        "ix_analytics_events_correlation",
        "analytics_events",
        ["correlation_id"],
        unique=False,
    )
    op.execute(_PROJECT_FUNCTION)
    op.execute(
        "CREATE TRIGGER trg_billing_outbox_analytics "
        "AFTER INSERT ON billing_outbox_events FOR EACH ROW "
        "EXECUTE FUNCTION project_billing_outbox_to_analytics()"
    )
    op.execute(_BACKFILL)


def downgrade() -> None:
    op.execute("DROP TRIGGER IF EXISTS trg_billing_outbox_analytics ON billing_outbox_events")
    op.execute("DROP FUNCTION IF EXISTS project_billing_outbox_to_analytics()")
    op.drop_index("ix_analytics_events_correlation", table_name="analytics_events")
    op.drop_index("ix_analytics_events_name_created", table_name="analytics_events")
    op.drop_table("analytics_events")
