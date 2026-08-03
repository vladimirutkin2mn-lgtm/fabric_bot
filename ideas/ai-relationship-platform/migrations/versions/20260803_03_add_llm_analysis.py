"""Add LLM analysis result and processing metadata.

Revision ID: 20260803_03
Revises: 20260802_02
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "20260803_03"
down_revision: str | None = "20260802_02"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("analyses", sa.Column("result_json", postgresql.JSONB(), nullable=True))
    for name, type_ in (
        ("llm_provider", sa.String(32)),
        ("model_name", sa.String(255)),
        ("prompt_version", sa.String(64)),
        ("provider_request_id", sa.String(255)),
        ("failure_code", sa.String(64)),
    ):
        op.add_column("analyses", sa.Column(name, type_, nullable=True))
    op.add_column(
        "analyses", sa.Column("llm_attempt_count", sa.Integer(), server_default="0", nullable=False)
    )
    for name in ("input_tokens", "output_tokens", "latency_ms"):
        op.add_column("analyses", sa.Column(name, sa.Integer(), nullable=True))
    op.add_column(
        "analyses", sa.Column("processing_started_at", sa.DateTime(timezone=True), nullable=True)
    )
    op.add_column("analyses", sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True))
    op.create_check_constraint(
        "ck_analyses_llm_metadata",
        "analyses",
        "llm_attempt_count >= 0 AND "
        "(input_tokens IS NULL OR input_tokens >= 0) AND "
        "(output_tokens IS NULL OR output_tokens >= 0) AND "
        "(latency_ms IS NULL OR latency_ms >= 0)",
    )
    op.create_check_constraint(
        "ck_analyses_terminal_result",
        "analyses",
        "(status <> 'completed' OR (result_json IS NOT NULL AND completed_at IS NOT NULL)) "
        "AND (status <> 'failed' OR (result_json IS NULL AND failure_code IS NOT NULL "
        "AND completed_at IS NULL))",
    )


def downgrade() -> None:
    op.drop_constraint("ck_analyses_terminal_result", "analyses", type_="check")
    op.drop_constraint("ck_analyses_llm_metadata", "analyses", type_="check")
    for name in (
        "completed_at",
        "processing_started_at",
        "failure_code",
        "provider_request_id",
        "latency_ms",
        "output_tokens",
        "input_tokens",
        "llm_attempt_count",
        "prompt_version",
        "model_name",
        "llm_provider",
        "result_json",
    ):
        op.drop_column("analyses", name)
