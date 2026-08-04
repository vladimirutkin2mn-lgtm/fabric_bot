"""Durable encrypted Telegram update inbox models."""

from datetime import datetime
from uuid import UUID

from sqlalchemy import (
    BigInteger,
    CheckConstraint,
    DateTime,
    Index,
    Integer,
    LargeBinary,
    String,
    func,
)
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base


class TelegramUpdateInbox(Base):
    """At-least-once Telegram delivery with encrypted transient payloads."""

    __tablename__ = "telegram_update_inbox"
    __table_args__ = (
        CheckConstraint(
            "status IN ('pending','claimed','completed','failed')",
            name="ck_telegram_update_inbox_status",
        ),
        CheckConstraint("attempt_count >= 0", name="ck_telegram_update_inbox_attempts"),
        CheckConstraint(
            "(status IN ('pending','claimed') AND payload_ciphertext IS NOT NULL "
            "AND payload_hash IS NOT NULL) OR "
            "(status IN ('completed','failed') AND payload_ciphertext IS NULL "
            "AND payload_hash IS NULL)",
            name="ck_telegram_update_inbox_payload_lifecycle",
        ),
        Index("ix_telegram_update_inbox_claimable", "status", "available_at"),
        Index("ix_telegram_update_inbox_user", "telegram_user_id"),
    )

    update_id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    telegram_user_id: Mapped[int | None] = mapped_column(BigInteger)
    payload_ciphertext: Mapped[bytes | None] = mapped_column(LargeBinary)
    payload_hash: Mapped[str | None] = mapped_column(String(64))
    status: Mapped[str] = mapped_column(String(16), default="pending", server_default="pending")
    attempt_count: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    available_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    claimed_by: Mapped[str | None] = mapped_column(String(255))
    claim_id: Mapped[UUID | None] = mapped_column(nullable=True)
    claimed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    lease_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_error_code: Mapped[str | None] = mapped_column(String(64))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
