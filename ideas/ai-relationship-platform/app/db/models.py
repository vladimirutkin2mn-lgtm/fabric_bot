"""Database models owned by the onboarding milestone."""
# ruff: noqa: E501

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
    LargeBinary,
    String,
    Text,
    UniqueConstraint,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.base import Base


class User(Base):
    """Telegram user and durable onboarding progress."""

    __tablename__ = "users"
    __table_args__ = (
        CheckConstraint(
            "(free_preview_status = 'available' AND free_preview_analysis_id IS NULL "
            "AND free_preview_used_at IS NULL) OR "
            "(free_preview_status = 'reserved' AND free_preview_analysis_id IS NOT NULL "
            "AND free_preview_used_at IS NULL) OR "
            "(free_preview_status = 'consumed' AND free_preview_used_at IS NOT NULL)",
            name="ck_users_free_preview",
        ),
    )

    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)
    telegram_user_id: Mapped[int | None] = mapped_column(BigInteger, unique=True, index=True)
    telegram_username: Mapped[str | None] = mapped_column(String(255))
    first_name: Mapped[str | None] = mapped_column(String(255))
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
    analyses: Mapped[list["Analysis"]] = relationship(
        back_populates="user", foreign_keys="Analysis.user_id"
    )
    free_preview_status: Mapped[str] = mapped_column(
        String(16), default="available", server_default="available"
    )
    free_preview_analysis_id: Mapped[UUID | None] = mapped_column(
        ForeignKey(
            "analyses.id",
            ondelete="SET NULL",
            use_alter=True,
            name="fk_users_preview_analysis",
        ),
        nullable=True,
    )
    free_preview_used_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    privacy_status: Mapped[str] = mapped_column(
        String(16), default="active", server_default="active"
    )
    deleted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class Analysis(Base):
    """Durable, privacy-sensitive conversation intake draft."""

    __tablename__ = "analyses"
    __table_args__ = (
        CheckConstraint(
            "status IN ('draft','queued','processing','completed','failed','deleted')",
            name="ck_analyses_status",
        ),
        CheckConstraint(
            "report_access IN ('none','preview','full')", name="ck_analyses_report_access"
        ),
        CheckConstraint("cost_units >= 0", name="ck_analyses_cost_units"),
        CheckConstraint(
            "(report_access = 'none') OR "
            "(report_access = 'preview' AND status = 'completed' AND cost_units = 0) OR "
            "(report_access = 'full' AND status = 'completed')",
            name="ck_analyses_access_state",
        ),
        CheckConstraint(
            "cost_units = 0 OR full_access_transaction_id IS NOT NULL",
            name="ck_analyses_paid_access_transaction",
        ),
        CheckConstraint(
            "status <> 'deleted' OR report_access = 'none'",
            name="ck_analyses_deleted_access",
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
            "(status <> 'completed' OR completed_at IS NOT NULL) "
            "AND (status <> 'failed' OR (result_json IS NULL AND failure_code IS NOT NULL "
            "AND completed_at IS NULL))",
            name="ck_analyses_terminal_result",
        ),
        CheckConstraint(
            "feedback_score IS NULL OR (feedback_score BETWEEN 1 AND 5 "
            "AND feedback_submitted_at IS NOT NULL)",
            name="ck_analyses_feedback",
        ),
        CheckConstraint(
            "status <> 'deleted' OR (feedback_score IS NULL AND feedback_submitted_at IS NULL)",
            name="ck_analyses_deleted_feedback",
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
    normalized_conversation_json: Mapped[list[dict[str, object]] | None] = mapped_column(
        JSON(none_as_null=True), nullable=True
    )
    participants_json: Mapped[dict[str, str] | None] = mapped_column(
        JSON(none_as_null=True), nullable=True
    )
    user_participant_label: Mapped[str | None] = mapped_column(String(8))
    user_goal: Mapped[str | None] = mapped_column(Text)
    relationship_stage: Mapped[str | None] = mapped_column(String(32))
    message_count: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    character_count: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    result_json: Mapped[dict[str, object] | None] = mapped_column(
        JSONB(none_as_null=True), nullable=True
    )
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
    feedback_score: Mapped[int | None] = mapped_column(Integer)
    feedback_submitted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    report_access: Mapped[str] = mapped_column(String(16), default="none", server_default="none")
    cost_units: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    full_access_transaction_id: Mapped[UUID | None] = mapped_column(
        ForeignKey(
            "credit_transactions.id",
            ondelete="RESTRICT",
            use_alter=True,
            name="fk_analyses_full_transaction",
        ),
        nullable=True,
    )
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )
    user: Mapped[User] = relationship(back_populates="analyses", foreign_keys=[user_id])
    deleted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    private_content: Mapped["AnalysisPrivateContent | None"] = relationship(
        back_populates="analysis", uselist=False, cascade="all, delete-orphan"
    )


class AnalysisPrivateContent(Base):
    """Authenticated ciphertext, separated from operational analysis metadata."""

    __tablename__ = "analysis_private_content"
    analysis_id: Mapped[UUID] = mapped_column(
        ForeignKey("analyses.id", ondelete="CASCADE"), primary_key=True
    )
    source_ciphertext: Mapped[bytes | None] = mapped_column(LargeBinary)
    result_ciphertext: Mapped[bytes | None] = mapped_column(LargeBinary)
    source_format_version: Mapped[int | None] = mapped_column(Integer)
    result_format_version: Mapped[int | None] = mapped_column(Integer)
    source_delete_after: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), index=True
    )
    source_deleted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    result_deleted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )
    analysis: Mapped[Analysis] = relationship(back_populates="private_content")


class PaymentOrder(Base):
    """Provider-neutral durable checkout; it never contains card data."""

    __tablename__ = "payment_orders"
    __table_args__ = (
        UniqueConstraint("provider", "provider_checkout_id", name="uq_payment_provider_checkout"),
        UniqueConstraint("provider", "provider_payment_id", name="uq_payment_provider_payment"),
        CheckConstraint(
            "status IN ('creating','pending','completed','failed','cancelled','manual_review')",
            name="ck_payment_orders_status",
        ),
        CheckConstraint("credits > 0 AND amount_minor > 0", name="ck_payment_orders_positive"),
        CheckConstraint("char_length(currency) = 3", name="ck_payment_orders_currency"),
        CheckConstraint(
            "(status = 'completed') = "
            "(completed_at IS NOT NULL AND provider_payment_id IS NOT NULL)",
            name="ck_payment_orders_completion",
        ),
        Index(
            "uq_payment_orders_active",
            "user_id",
            "provider",
            "product_code",
            "market",
            "currency",
            unique=True,
            postgresql_where=text("status IN ('creating','pending')"),
        ),
    )
    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)
    user_id: Mapped[UUID] = mapped_column(ForeignKey("users.id", ondelete="RESTRICT"), index=True)
    provider: Mapped[str] = mapped_column(String(32))
    product_code: Mapped[str] = mapped_column(String(64))
    status: Mapped[str] = mapped_column(String(16), default="creating")
    credits: Mapped[int] = mapped_column(Integer)
    amount_minor: Mapped[int] = mapped_column(Integer)
    currency: Mapped[str] = mapped_column(String(3))
    checkout_token: Mapped[UUID] = mapped_column(unique=True, default=uuid4)
    checkout_url: Mapped[str | None] = mapped_column(String(2048))
    checkout_creation_attempt_id: Mapped[UUID | None] = mapped_column(nullable=True)
    checkout_creation_started_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    checkout_started_emitted: Mapped[bool] = mapped_column(
        Boolean, default=False, server_default="false"
    )
    provider_checkout_id: Mapped[str | None] = mapped_column(String(255))
    provider_payment_id: Mapped[str | None] = mapped_column(String(255))
    provider_event_id: Mapped[str | None] = mapped_column(String(255), unique=True)
    mode: Mapped[str] = mapped_column(String(32), default="one_time", server_default="one_time")
    market: Mapped[str] = mapped_column(String(32), default="RU", server_default="RU")
    product_version: Mapped[int] = mapped_column(Integer, default=1, server_default="1")
    billing_period: Mapped[str | None] = mapped_column(String(32))
    provider_invoice_id: Mapped[str | None] = mapped_column(String(255))
    subscription_id: Mapped[UUID | None] = mapped_column(
        ForeignKey(
            "subscriptions.id",
            ondelete="RESTRICT",
            use_alter=True,
            name="fk_payment_orders_subscription",
        )
    )
    provider_status: Mapped[str | None] = mapped_column(String(64))
    idempotency_key: Mapped[str | None] = mapped_column(String(255), unique=True)
    provider_request_id: Mapped[str | None] = mapped_column(String(255))
    failure_code: Mapped[str | None] = mapped_column(String(64))
    commercial_snapshot: Mapped[dict[str, object]] = mapped_column(
        JSONB, default=dict, server_default="{}"
    )
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    checkout_expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_reconciled_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    provider_live_mode: Mapped[bool | None] = mapped_column(Boolean)
    encrypted_receipt_contact: Mapped[bytes | None] = mapped_column(LargeBinary)


class CreditTransaction(Base):
    """Append-only integer credit ledger."""

    __tablename__ = "credit_transactions"
    __table_args__ = (
        CheckConstraint("amount <> 0", name="ck_credit_transactions_nonzero"),
        CheckConstraint(
            "type IN ('grant','purchase','spend','refund','adjustment','purchase_refund')",
            name="ck_credit_transactions_type",
        ),
        CheckConstraint(
            "(type IN ('grant','purchase','refund') AND amount > 0) OR (type = 'purchase_refund' AND amount < 0) OR "
            "(type = 'spend' AND amount < 0) OR "
            "(type = 'adjustment' AND amount <> 0)",
            name="ck_credit_transactions_sign",
        ),
        CheckConstraint(
            "type <> 'spend' OR analysis_id IS NOT NULL",
            name="ck_credit_transactions_spend_analysis",
        ),
        CheckConstraint(
            "type <> 'purchase' OR (payment_order_id IS NOT NULL AND product_code IS NOT NULL)",
            name="ck_credit_transactions_purchase_order",
        ),
        CheckConstraint(
            "type <> 'refund' OR reverses_transaction_id IS NOT NULL",
            name="ck_credit_transactions_refund_reversal",
        ),
        CheckConstraint(
            "type <> 'purchase_refund' OR (original_purchase_transaction_id IS NOT NULL "
            "AND payment_order_id IS NOT NULL AND refund_request_id IS NOT NULL)",
            name="ck_credit_transactions_purchase_refund_refs",
        ),
    )
    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)
    user_id: Mapped[UUID] = mapped_column(ForeignKey("users.id", ondelete="RESTRICT"), index=True)
    type: Mapped[str] = mapped_column(String(16))
    amount: Mapped[int] = mapped_column(Integer)
    idempotency_key: Mapped[str] = mapped_column(String(255), unique=True)
    analysis_id: Mapped[UUID | None] = mapped_column(ForeignKey("analyses.id", ondelete="RESTRICT"))
    payment_order_id: Mapped[UUID | None] = mapped_column(
        ForeignKey("payment_orders.id", ondelete="RESTRICT"), unique=True
    )
    reverses_transaction_id: Mapped[UUID | None] = mapped_column(
        ForeignKey("credit_transactions.id", ondelete="RESTRICT"), unique=True
    )
    product_code: Mapped[str | None] = mapped_column(String(64))
    external_payment_id: Mapped[str | None] = mapped_column(String(255), unique=True)
    original_purchase_transaction_id: Mapped[UUID | None] = mapped_column(
        ForeignKey("credit_transactions.id", ondelete="RESTRICT")
    )
    refund_request_id: Mapped[UUID | None] = mapped_column(
        ForeignKey(
            "refund_requests.id",
            ondelete="RESTRICT",
            use_alter=True,
            name="fk_credit_transactions_refund_request",
        ),
        unique=True,
    )
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class BillingCustomer(Base):
    __tablename__ = "billing_customers"
    __table_args__ = (
        UniqueConstraint("user_id", "provider"),
        UniqueConstraint("provider", "provider_customer_id"),
    )
    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)
    user_id: Mapped[UUID] = mapped_column(ForeignKey("users.id", ondelete="RESTRICT"), index=True)
    provider: Mapped[str] = mapped_column(String(32))
    provider_customer_id: Mapped[str | None] = mapped_column(String(255))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )


class Subscription(Base):
    __tablename__ = "subscriptions"
    __table_args__ = (
        CheckConstraint(
            "status IN ('incomplete','active','past_due','cancel_at_period_end','canceled','unpaid','paused')",
            name="ck_subscriptions_status",
        ),
        Index(
            "uq_subscriptions_active_user_product",
            "user_id",
            "product_code",
            unique=True,
            postgresql_where=text(
                "status IN ('incomplete','active','past_due','cancel_at_period_end','paused')"
            ),
        ),
    )
    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)
    user_id: Mapped[UUID] = mapped_column(ForeignKey("users.id", ondelete="RESTRICT"), index=True)
    billing_customer_id: Mapped[UUID] = mapped_column(
        ForeignKey("billing_customers.id", ondelete="RESTRICT")
    )
    provider: Mapped[str] = mapped_column(String(32))
    provider_subscription_id: Mapped[str] = mapped_column(String(255), unique=True)
    product_code: Mapped[str] = mapped_column(String(64))
    product_version: Mapped[int] = mapped_column(Integer)
    status: Mapped[str] = mapped_column(String(32), default="incomplete")
    encrypted_payment_method: Mapped[bytes | None] = mapped_column(LargeBinary)
    current_period_start: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    current_period_end: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    cancel_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    canceled_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    consent_version: Mapped[str] = mapped_column(String(64))
    consented_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    renewal_claimed_by: Mapped[str | None] = mapped_column(String(255))
    renewal_lease_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_order_id: Mapped[UUID | None] = mapped_column(
        ForeignKey(
            "payment_orders.id",
            ondelete="RESTRICT",
            use_alter=True,
            name="fk_subscriptions_last_order",
        )
    )
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )


class ProviderWebhookEvent(Base):
    __tablename__ = "provider_webhook_events"
    __table_args__ = (
        UniqueConstraint("provider", "provider_event_id"),
        CheckConstraint(
            "status IN ('pending','processing','completed','failed','manual_review')",
            name="ck_webhook_status",
        ),
    )
    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)
    provider: Mapped[str] = mapped_column(String(32))
    provider_event_id: Mapped[str] = mapped_column(String(255))
    event_type: Mapped[str] = mapped_column(String(128))
    provider_object_id: Mapped[str] = mapped_column(String(255))
    payload_hash: Mapped[str] = mapped_column(String(64))
    status: Mapped[str] = mapped_column(String(32), default="pending")
    attempt_count: Mapped[int] = mapped_column(Integer, default=0)
    received_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    processed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_error_code: Mapped[str | None] = mapped_column(String(64))
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )


class RefundRequest(Base):
    __tablename__ = "refund_requests"
    __table_args__ = (
        CheckConstraint(
            "status IN ('requested','credits_reserved','provider_pending','succeeded','failed','manual_review')",
            name="ck_refunds_status",
        ),
        CheckConstraint("amount_minor > 0 AND credit_units > 0", name="ck_refunds_positive"),
    )
    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)
    user_id: Mapped[UUID] = mapped_column(ForeignKey("users.id", ondelete="RESTRICT"), index=True)
    payment_order_id: Mapped[UUID] = mapped_column(
        ForeignKey("payment_orders.id", ondelete="RESTRICT")
    )
    provider: Mapped[str] = mapped_column(String(32))
    provider_refund_id: Mapped[str | None] = mapped_column(String(255), unique=True)
    status: Mapped[str] = mapped_column(String(32), default="requested")
    amount_minor: Mapped[int] = mapped_column(Integer)
    currency: Mapped[str] = mapped_column(String(3))
    credit_units: Mapped[int] = mapped_column(Integer)
    reason: Mapped[str] = mapped_column(String(255))
    idempotency_key: Mapped[str] = mapped_column(String(255), unique=True)
    provider_request_id: Mapped[str | None] = mapped_column(String(255))
    failure_code: Mapped[str | None] = mapped_column(String(64))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )


class CreditReservation(Base):
    __tablename__ = "credit_reservations"
    __table_args__ = (
        CheckConstraint("credit_units > 0", name="ck_reservations_positive"),
        CheckConstraint(
            "status IN ('active','consumed','released')", name="ck_reservations_status"
        ),
    )
    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)
    user_id: Mapped[UUID] = mapped_column(ForeignKey("users.id", ondelete="RESTRICT"), index=True)
    refund_request_id: Mapped[UUID] = mapped_column(
        ForeignKey("refund_requests.id", ondelete="RESTRICT"), unique=True
    )
    credit_units: Mapped[int] = mapped_column(Integer)
    status: Mapped[str] = mapped_column(String(16), default="active")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )


class BillingJob(Base):
    __tablename__ = "billing_jobs"
    __table_args__ = (
        CheckConstraint(
            "job_type IN ('webhook_processing','subscription_renewal','payment_reconciliation','refund_reconciliation')",
            name="ck_billing_jobs_type",
        ),
        CheckConstraint(
            "status IN ('pending','claimed','completed','failed','manual_review')",
            name="ck_billing_jobs_status",
        ),
    )
    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)
    job_type: Mapped[str] = mapped_column(String(32))
    provider: Mapped[str] = mapped_column(String(32))
    object_type: Mapped[str] = mapped_column(String(64))
    object_id: Mapped[str] = mapped_column(String(255))
    idempotency_key: Mapped[str] = mapped_column(String(255), unique=True)
    status: Mapped[str] = mapped_column(String(32), default="pending")
    attempt_count: Mapped[int] = mapped_column(Integer, default=0)
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


class BillingOutboxEvent(Base):
    """Append-only transactional handoff for non-financial side effects."""

    __tablename__ = "billing_outbox_events"
    __table_args__ = (
        CheckConstraint(
            "status IN ('pending','claimed','completed','failed','manual_review')",
            name="ck_billing_outbox_status",
        ),
    )
    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)
    aggregate_type: Mapped[str] = mapped_column(String(64))
    aggregate_id: Mapped[str] = mapped_column(String(255))
    event_type: Mapped[str] = mapped_column(String(64))
    payload: Mapped[dict[str, object]] = mapped_column(JSONB, default=dict, server_default="{}")
    idempotency_key: Mapped[str] = mapped_column(String(255), unique=True)
    status: Mapped[str] = mapped_column(String(32), default="pending")
    attempt_count: Mapped[int] = mapped_column(Integer, default=0)
    available_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    claimed_by: Mapped[str | None] = mapped_column(String(255))
    claim_id: Mapped[UUID | None] = mapped_column(nullable=True)
    claimed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    lease_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_error_code: Mapped[str | None] = mapped_column(String(64))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
