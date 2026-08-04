"""Durable aiogram FSM records."""

from datetime import datetime

from sqlalchemy import BigInteger, DateTime, Index, LargeBinary, String, func
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base


class TelegramFSMState(Base):
    """One encrypted FSM record for an aiogram storage key."""

    __tablename__ = "telegram_fsm_state"
    __table_args__ = (Index("ix_telegram_fsm_state_user", "user_id"),)

    bot_id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    chat_id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    user_id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    thread_id: Mapped[int] = mapped_column(BigInteger, primary_key=True, default=0)
    business_connection_id: Mapped[str] = mapped_column(String(255), primary_key=True, default="")
    destiny: Mapped[str] = mapped_column(String(255), primary_key=True, default="default")
    state: Mapped[str | None] = mapped_column(String(255))
    data_ciphertext: Mapped[bytes | None] = mapped_column(LargeBinary)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )
