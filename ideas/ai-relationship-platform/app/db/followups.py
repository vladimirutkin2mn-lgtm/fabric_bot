"""Durable, encrypted entitlement for the one included paid follow-up question."""

from datetime import datetime
from uuid import UUID, uuid4

from sqlalchemy import (
    CheckConstraint,
    DDL,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    LargeBinary,
    String,
    UniqueConstraint,
    event,
    func,
    text,
)
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base


class FollowUpQuestion(Base):
    """One claim-fenced follow-up entitlement per paid full-access analysis."""

    __tablename__ = "analysis_followups"
    __table_args__ = (
        UniqueConstraint("analysis_id", name="uq_analysis_followups_analysis"),
        CheckConstraint(
            "status IN ('available','reserved','completed')",
            name="ck_analysis_followups_status",
        ),
        CheckConstraint(
            "reservation_count >= 0 AND llm_attempt_count >= 0",
            name="ck_analysis_followups_attempts",
        ),
        CheckConstraint(
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
        Index(
            "ix_analysis_followups_expired_reservations",
            "lease_until",
            postgresql_where=text("status = 'reserved'"),
        ),
    )

    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)
    analysis_id: Mapped[UUID] = mapped_column(
        ForeignKey("analyses.id", ondelete="CASCADE"), nullable=False
    )
    user_id: Mapped[UUID] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True
    )
    status: Mapped[str] = mapped_column(String(16), default="available", server_default="available")
    claim_id: Mapped[UUID | None] = mapped_column(nullable=True)
    lease_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    question_ciphertext: Mapped[bytes | None] = mapped_column(LargeBinary)
    answer_ciphertext: Mapped[bytes | None] = mapped_column(LargeBinary)
    prompt_version: Mapped[str] = mapped_column(String(64), default="followup_v1")
    reservation_count: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    llm_attempt_count: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    llm_provider: Mapped[str | None] = mapped_column(String(32))
    model_name: Mapped[str | None] = mapped_column(String(255))
    provider_request_id: Mapped[str | None] = mapped_column(String(255))
    input_tokens: Mapped[int | None] = mapped_column(Integer)
    output_tokens: Mapped[int | None] = mapped_column(Integer)
    latency_ms: Mapped[int | None] = mapped_column(Integer)
    last_failure_code: Mapped[str | None] = mapped_column(String(64))
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )


_PURGE_FUNCTION = DDL(
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
_DROP_PURGE_TRIGGER = DDL(
    "DROP TRIGGER IF EXISTS trg_purge_analysis_followup_on_delete ON analyses"
)
_CREATE_PURGE_TRIGGER = DDL(
    """
    CREATE TRIGGER trg_purge_analysis_followup_on_delete
    AFTER UPDATE OF status ON analyses
    FOR EACH ROW EXECUTE FUNCTION purge_analysis_followup_on_delete()
    """
)
_DROP_PURGE_FUNCTION = DDL("DROP FUNCTION IF EXISTS purge_analysis_followup_on_delete()")

for statement in (_PURGE_FUNCTION, _DROP_PURGE_TRIGGER, _CREATE_PURGE_TRIGGER):
    event.listen(FollowUpQuestion.__table__, "after_create", statement)
for statement in (_DROP_PURGE_TRIGGER, _DROP_PURGE_FUNCTION):
    event.listen(FollowUpQuestion.__table__, "before_drop", statement)
