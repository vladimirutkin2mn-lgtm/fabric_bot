"""PostgreSQL and HTTP coverage for aggregate-only administration metrics."""

import os
from collections.abc import AsyncIterator
from datetime import UTC, datetime
from uuid import uuid4

import pytest
from httpx import ASGITransport, AsyncClient
from pydantic import SecretStr
from sqlalchemy.ext.asyncio import AsyncEngine, async_sessionmaker, create_async_engine

from app.api.main import create_app
from app.config import Settings
from app.db.analytics import AnalyticsEvent
from app.db.base import Base
from app.db.models import (
    Analysis,
    BillingJob,
    BillingOutboxEvent,
    CreditTransaction,
    PaymentOrder,
    User,
)
from app.observability.settings import ObservabilitySettings

pytestmark = pytest.mark.postgres


@pytest.fixture
async def admin_engine() -> AsyncIterator[AsyncEngine]:
    url = os.getenv("TEST_DATABASE_URL")
    if not url:
        pytest.skip("TEST_DATABASE_URL is required")
    engine = create_async_engine(url)
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.drop_all)
        await connection.run_sync(Base.metadata.create_all)
    yield engine
    await engine.dispose()


def _settings(url: str) -> Settings:
    return Settings(
        app_env="test",
        database_url=url,
        telegram_bot_token=SecretStr("123456789:AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA"),
        content_encryption_key=SecretStr("admin-observability-test-key"),
    )


async def _seed_metrics(engine: AsyncEngine) -> str:
    sentinel = "private-admin-sentinel"
    now = datetime.now(UTC)
    user_id, payment_order_id = uuid4(), uuid4()
    sessions = async_sessionmaker(engine, expire_on_commit=False)
    async with sessions.begin() as session:
        session.add(
            User(
                id=user_id,
                telegram_user_id=991001,
                telegram_username=sentinel,
                first_name=sentinel,
                privacy_status="active",
            )
        )
        session.add_all(
            [
                Analysis(
                    user_id=user_id,
                    status="completed",
                    intake_step="complete",
                    normalized_conversation_json=[{"text": sentinel}],
                    message_count=4,
                    character_count=100,
                    llm_attempt_count=1,
                    input_tokens=10,
                    output_tokens=20,
                    latency_ms=100,
                    completed_at=now,
                    report_access="none",
                    cost_units=0,
                ),
                Analysis(
                    user_id=user_id,
                    status="completed",
                    intake_step="complete",
                    message_count=4,
                    character_count=100,
                    llm_attempt_count=1,
                    input_tokens=30,
                    output_tokens=40,
                    latency_ms=300,
                    completed_at=now,
                    report_access="none",
                    cost_units=0,
                ),
                Analysis(
                    user_id=user_id,
                    status="failed",
                    intake_step="complete",
                    message_count=4,
                    character_count=100,
                    llm_attempt_count=2,
                    input_tokens=5,
                    output_tokens=5,
                    latency_ms=200,
                    failure_code="llm_timeout",
                    report_access="none",
                    cost_units=0,
                ),
                Analysis(user_id=user_id, status="draft", intake_step="complete"),
            ]
        )
        session.add(
            PaymentOrder(
                id=payment_order_id,
                user_id=user_id,
                provider="stripe",
                product_code="analysis_pack_5",
                status="completed",
                credits=5,
                amount_minor=69900,
                currency="RUB",
                mode="one_time",
                market="RU",
                product_version=1,
                commercial_snapshot={},
                completed_at=now,
            )
        )
        session.add(
            CreditTransaction(
                user_id=user_id,
                type="purchase",
                amount=5,
                idempotency_key=f"purchase:{payment_order_id}",
                payment_order_id=payment_order_id,
                product_code="analysis_pack_5",
            )
        )
        session.add_all(
            [
                AnalyticsEvent(
                    event_name="bot_started",
                    subject_id=str(user_id),
                    properties={},
                    idempotency_key=f"bot_started:{user_id}",
                    correlation_id="seed-one",
                ),
                AnalyticsEvent(
                    event_name="conversation_rejected",
                    subject_id=str(user_id),
                    properties={"rejection_reason": "too_short"},
                    idempotency_key="conversation_rejected:seed-two",
                    correlation_id="seed-two",
                ),
                AnalyticsEvent(
                    event_name="conversation_rejected",
                    subject_id=str(user_id),
                    properties={"rejection_reason": "one_participant"},
                    idempotency_key="conversation_rejected:seed-three",
                    correlation_id="seed-three",
                ),
            ]
        )
        session.add_all(
            [
                BillingJob(
                    job_type="payment_reconciliation",
                    provider="stripe",
                    object_type="payment_order",
                    object_id=str(uuid4()),
                    idempotency_key=f"job:{uuid4()}",
                    status="pending",
                ),
                BillingJob(
                    job_type="payment_reconciliation",
                    provider="stripe",
                    object_type="payment_order",
                    object_id=str(uuid4()),
                    idempotency_key=f"job:{uuid4()}",
                    status="manual_review",
                ),
                BillingOutboxEvent(
                    aggregate_type="payment_order",
                    aggregate_id=str(uuid4()),
                    event_type="purchase_completed",
                    payload={},
                    idempotency_key=f"outbox:{uuid4()}",
                    status="claimed",
                ),
                BillingOutboxEvent(
                    aggregate_type="payment_order",
                    aggregate_id=str(uuid4()),
                    event_type="payment_failed",
                    payload={},
                    idempotency_key=f"outbox:{uuid4()}",
                    status="manual_review",
                ),
            ]
        )
    return sentinel


async def test_admin_metrics_require_token_and_return_only_aggregates(
    admin_engine: AsyncEngine,
) -> None:
    url = os.getenv("TEST_DATABASE_URL")
    assert url is not None
    sentinel = await _seed_metrics(admin_engine)
    observability = ObservabilitySettings(
        app_env="test",
        admin_metrics_enabled=True,
        admin_api_token=SecretStr("admin-test-token"),
    )
    app = create_app(_settings(url), admin_engine, observability)

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        missing = await client.get("/admin/metrics")
        wrong = await client.get("/admin/metrics", headers={"X-Admin-Token": "wrong"})
        response = await client.get(
            "/admin/metrics",
            headers={
                "X-Admin-Token": "admin-test-token",
                "X-Correlation-ID": "admin-request-1",
            },
        )

    assert missing.status_code == 401
    assert wrong.status_code == 401
    assert response.status_code == 200
    assert response.headers["x-correlation-id"] == "admin-request-1"
    payload = response.json()
    assert payload["analyses_by_status"] == {
        "completed": 2,
        "draft": 1,
        "failed": 1,
    }
    assert payload["terminal_completed"] == 2
    assert payload["terminal_failed"] == 1
    assert payload["completion_rate"] == pytest.approx(2 / 3)
    assert payload["model_usage"] == {
        "average_latency_ms": 200.0,
        "average_input_tokens": 15.0,
        "average_output_tokens": pytest.approx(65 / 3),
        "average_total_tokens": pytest.approx(110 / 3),
        "average_cost_units": 0.0,
    }
    assert payload["purchases"] == {
        "transaction_count": 1,
        "purchased_credit_total": 5,
    }
    assert payload["funnel_events"]["bot_started"] == 1
    assert payload["funnel_events"]["conversation_rejected"] == 2
    assert payload["failures"] == {
        "user_validation_total": 2,
        "technical_total": 1,
        "conversation_rejection_reasons": {"one_participant": 1, "too_short": 1},
        "analysis_failure_codes": {"llm_timeout": 1},
    }
    assert payload["billing_jobs_by_status"] == {"manual_review": 1, "pending": 1}
    assert payload["billing_outbox_by_status"] == {"claimed": 1, "manual_review": 1}
    assert sentinel not in response.text


async def test_admin_metrics_are_hidden_when_disabled(admin_engine: AsyncEngine) -> None:
    url = os.getenv("TEST_DATABASE_URL")
    assert url is not None
    app = create_app(
        _settings(url),
        admin_engine,
        ObservabilitySettings(app_env="test", admin_metrics_enabled=False),
    )
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.get("/admin/metrics")
    assert response.status_code == 404


async def test_invalid_correlation_header_is_replaced(admin_engine: AsyncEngine) -> None:
    url = os.getenv("TEST_DATABASE_URL")
    assert url is not None
    app = create_app(_settings(url), admin_engine, ObservabilitySettings(app_env="test"))
    private_header = "private correlation with spaces " * 10
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.get("/health/live", headers={"X-Correlation-ID": private_header})
    generated = response.headers["x-correlation-id"]
    assert response.status_code == 200
    assert len(generated) == 32
    assert generated.isalnum()
    assert "private" not in generated
