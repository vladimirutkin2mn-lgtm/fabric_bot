"""Append-only staging gate results for release readiness."""

from datetime import datetime
from uuid import UUID, uuid4

from sqlalchemy import CheckConstraint, DateTime, Index, String, func
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base


class ReleaseGateAttestation(Base):
    """Immutable result for an exact code, schema, and checklist tuple."""

    __tablename__ = "release_gate_attestations"
    __table_args__ = (
        CheckConstraint("status IN ('passed','failed')", name="ck_release_gate_status"),
        CheckConstraint("app_env = 'staging'", name="ck_release_gate_environment"),
        CheckConstraint(
            "char_length(evidence_ref) BETWEEN 1 AND 512",
            name="ck_release_gate_evidence",
        ),
        Index("ix_release_gate_latest", "gate_name", "attested_at"),
    )

    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)
    gate_name: Mapped[str] = mapped_column(String(64), nullable=False)
    status: Mapped[str] = mapped_column(String(16), nullable=False)
    checklist_version: Mapped[str] = mapped_column(String(64), nullable=False)
    app_env: Mapped[str] = mapped_column(String(16), nullable=False)
    code_sha: Mapped[str] = mapped_column(String(64), nullable=False)
    schema_revision: Mapped[str] = mapped_column(String(64), nullable=False)
    evidence_ref: Mapped[str] = mapped_column(String(512), nullable=False)
    attested_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
