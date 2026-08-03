"""Database models owned by the onboarding milestone."""

from datetime import datetime
from uuid import UUID, uuid4

from sqlalchemy import (
    JSON,
    BigInteger,
    Boolean,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    func,
    text,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.base import Base


class User(Base):
    """Telegram user and durable onboarding progress."""

    __tablename__ = "users"

    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)
    telegram_user_id: Mapped[int] = mapped_column(BigInteger, unique=True, index=True)
    telegram_username: Mapped[str | None] = mapped_column(String(255))
    first_name: Mapped[str] = mapped_column(String(255))
    telegram_language: Mapped[str | None] = mapped_column(String(16))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )
    age_confirmed: Mapped[bool] = mapped_column(Boolean, default=False, server_default="false")
    age_confirmed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    consent_version: Mapped[str | None] = mapped_column(String(32))
    consent_accepted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    onboarding_completed: Mapped[bool] = mapped_column(
        Boolean, default=False, server_default="false"
    )
    analyses: Mapped[list["Analysis"]] = relationship(back_populates="user")


class Analysis(Base):
    """Durable, privacy-sensitive conversation intake draft."""

    __tablename__ = "analyses"
    __table_args__ = (
        CheckConstraint(
            "status IN ('draft','queued','processing','completed','failed','deleted')",
            name="ck_analyses_status",
        ),
        CheckConstraint(
            "intake_step IN ('waiting_for_conversation','waiting_for_participant',"
            "'waiting_for_goal','waiting_for_relationship_stage','complete')",
            name="ck_analyses_intake_step",
        ),
        CheckConstraint("message_count >= 0 AND character_count >= 0", name="ck_analyses_counts"),
        CheckConstraint(
            "llm_attempt_count >= 0 AND (input_tokens IS NULL OR input_tokens >= 0) "
            "AND (output_tokens IS NULL OR output_tokens >= 0) "
            "AND (latency_ms IS NULL OR latency_ms >= 0)",
            name="ck_analyses_llm_metadata",
        ),
        CheckConstraint(
            "(status <> 'completed' OR result_json IS NOT NULL) AND "
            "(status <> 'failed' OR result_json IS NULL)",
            name="ck_analyses_terminal_result",
        ),
        Index(
            "uq_analyses_active_draft_user",
            "user_id",
            unique=True,
            postgresql_where=text("status = 'draft' AND intake_step <> 'complete'"),
        ),
    )

    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)
    user_id: Mapped[UUID] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    status: Mapped[str] = mapped_column(String(16), default="draft", server_default="draft")
    intake_step: Mapped[str] = mapped_column(String(40), default="waiting_for_conversation")
    source_type: Mapped[str] = mapped_column(String(20), default="text", server_default="text")
    normalized_conversation_json: Mapped[list[dict[str, object]] | None] = mapped_column(JSON)
    participants_json: Mapped[dict[str, str] | None] = mapped_column(JSON)
    user_participant_label: Mapped[str | None] = mapped_column(String(8))
    user_goal: Mapped[str | None] = mapped_column(Text)
    relationship_stage: Mapped[str | None] = mapped_column(String(32))
    message_count: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    character_count: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    result_json: Mapped[dict[str, object] | None] = mapped_column(JSON)
    llm_provider: Mapped[str | None] = mapped_column(String(32))
    model_name: Mapped[str | None] = mapped_column(String(255))
    prompt_version: Mapped[str | None] = mapped_column(String(64))
    llm_attempt_count: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    input_tokens: Mapped[int | None] = mapped_column(Integer)
    output_tokens: Mapped[int | None] = mapped_column(Integer)
    latency_ms: Mapped[int | None] = mapped_column(Integer)
    provider_request_id: Mapped[str | None] = mapped_column(String(255))
    processing_started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    failure_code: Mapped[str | None] = mapped_column(String(64))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )
    user: Mapped[User] = relationship(back_populates="analyses")
