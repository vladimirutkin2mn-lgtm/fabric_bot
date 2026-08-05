"""PostgreSQL and HTTP coverage for staging release gates."""

import os
from collections.abc import AsyncIterator
from uuid import uuid4

import pytest
from httpx import ASGITransport, AsyncClient
from pydantic import SecretStr
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine, async_sessionmaker, create_async_engine

from app.api.main import create_app
from app.config import Settings
from app.db.base import Base
from app.db.models import BillingJob
from app.observability.settings import ObservabilitySettings
from app.services.release_readiness import ReleaseGateName, ReleaseReadiness

pytestmark = pytest.mark.postgres


@pytest.fixture
async def release_engine() -> AsyncIterator[AsyncEngine]:
    url = os.getenv("TEST_DATABASE_URL")
    if not url:
        pytest.skip("TEST_DATABASE_URL is required")
    engine = create_async_engine(url)
    async with engine.begin() as connection:
        await connection.execute(text("DROP TABLE IF EXISTS alembic_version"))
        await connection.run_sync(Base.metadata.drop_all)
        await connection.run_sync(Base.metadata.create_all)
        await connection.execute(
            text("CREATE TABLE alembic_version (version_num VARCHAR(64) PRIMARY KEY)")
        )
        await connection.execute(
            text("INSERT INTO alembic_version (version_num) VALUES ('20260805_16')")
        )
    yield engine
    async with engine.begin() as connection:
        await connection.execute(text("DROP TABLE IF EXISTS alembic_version"))
        await connection.run_sync(Base.metadata.drop_all)
    await engine.dispose()


def _settings(url: str) -> Settings:
    return Settings(
        app_env="staging",
        database_url=url,
        telegram_bot_token=SecretStr("123456789:AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA"),
        content_encryption_key=SecretStr("release-readiness-test-key-material"),
        billing_enabled=True,
        payment_public_base_url="https://staging.example.test",
        stripe_enabled=True,
        stripe_secret_key=SecretStr("sk_test_release_gate"),
        stripe_webhook_secret=SecretStr("whsec_release_gate"),
        stripe_price_subscription_monthly_eur="price_release_gate",
        stripe_amount_subscription_monthly_eur_minor=990,
        subscriptions_enabled=True,
        yookassa_enabled=True,
        yookassa_recurring_enabled=True,
        yookassa_shop_id=SecretStr("test-shop"),
        yookassa_secret_key=SecretStr("test-secret"),
        yookassa_webhook_ip_allowlist="127.0.0.1/32",
        refunds_enabled=True,
        llm_provider="openai",
        openai_api_key=SecretStr("test-openai-key"),
        llm_model="gpt-test",
    )


def _observability() -> ObservabilitySettings:
    return ObservabilitySettings(
        app_env="staging",
        admin_metrics_enabled=True,
        admin_api_token=SecretStr("release-admin-token"),
    )


def _headers() -> dict[str, str]:
    return {"X-Admin-Token": "release-admin-token"}


async def _pass_all_gates(client: AsyncClient) -> ReleaseReadiness:
    payload: ReleaseReadiness | None = None
    for index, gate in enumerate(ReleaseGateName, start=1):
        response = await client.post(
            f"/admin/release-gates/{gate.value}",
            headers=_headers(),
            json={"status": "passed", "evidence_ref": f"staging/run-{index}"},
        )
        assert response.status_code == 200, response.text
        payload = ReleaseReadiness.model_validate(response.json())
    assert payload is not None
    return payload


async def test_release_gates_require_admin_auth_and_current_evidence(
    release_engine: AsyncEngine,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    url = os.getenv("TEST_DATABASE_URL")
    assert url is not None
    monkeypatch.setenv("RELEASE_CODE_SHA", "a" * 40)
    monkeypatch.setenv("RELEASE_CHECKLIST_VERSION", "m5-live-v1")
    app = create_app(_settings(url), release_engine, _observability())

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        missing = await client.get("/admin/release-readiness")
        initial_response = await client.get("/admin/release-readiness", headers=_headers())
        final_payload = await _pass_all_gates(client)

    assert missing.status_code == 401
    assert initial_response.status_code == 200
    initial = ReleaseReadiness.model_validate(initial_response.json())
    assert initial.ready_for_limited_production is False
    assert {gate.state.value for gate in initial.gates} == {"missing"}
    assert final_payload.ready_for_limited_production is True
    assert final_payload.blockers == []
    assert final_payload.schema_revision == "20260805_16"
    assert {gate.state.value for gate in final_payload.gates} == {"passed"}


async def test_new_code_sha_invalidates_previous_passes(
    release_engine: AsyncEngine,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    url = os.getenv("TEST_DATABASE_URL")
    assert url is not None
    monkeypatch.setenv("RELEASE_CODE_SHA", "b" * 40)
    monkeypatch.setenv("RELEASE_CHECKLIST_VERSION", "m5-live-v1")
    app = create_app(_settings(url), release_engine, _observability())

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        await _pass_all_gates(client)
        monkeypatch.setenv("RELEASE_CODE_SHA", "c" * 40)
        response = await client.get("/admin/release-readiness", headers=_headers())

    assert response.status_code == 200
    payload = ReleaseReadiness.model_validate(response.json())
    assert payload.ready_for_limited_production is False
    assert {gate.state.value for gate in payload.gates} == {"stale"}
    assert all(gate.current_code is False for gate in payload.gates)


async def test_financial_manual_review_recloses_ready_release(
    release_engine: AsyncEngine,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    url = os.getenv("TEST_DATABASE_URL")
    assert url is not None
    monkeypatch.setenv("RELEASE_CODE_SHA", "d" * 40)
    app = create_app(_settings(url), release_engine, _observability())

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        ready = await _pass_all_gates(client)
        assert ready.ready_for_limited_production is True
        sessions = async_sessionmaker(release_engine, expire_on_commit=False)
        async with sessions.begin() as session:
            session.add(
                BillingJob(
                    job_type="payment_reconciliation",
                    provider="stripe",
                    object_type="payment_order",
                    object_id=str(uuid4()),
                    idempotency_key=f"release-gate:{uuid4()}",
                    status="manual_review",
                )
            )
        response = await client.get("/admin/release-readiness", headers=_headers())

    payload = ReleaseReadiness.model_validate(response.json())
    assert payload.ready_for_limited_production is False
    assert payload.financial_blockers["billing_jobs_manual_review"] == 1
    assert "billing_jobs_manual_review" in payload.blockers


async def test_pass_attestation_refuses_incomplete_gate_configuration(
    release_engine: AsyncEngine,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    url = os.getenv("TEST_DATABASE_URL")
    assert url is not None
    monkeypatch.setenv("RELEASE_CODE_SHA", "e" * 40)
    incomplete = _settings(url).model_copy(update={"refunds_enabled": False})
    app = create_app(incomplete, release_engine, _observability())

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.post(
            "/admin/release-gates/stripe_refund_sandbox",
            headers=_headers(),
            json={"status": "passed", "evidence_ref": "staging/refund-run"},
        )

    assert response.status_code == 409
    assert response.json()["detail"] == {
        "code": "gate_configuration_incomplete",
        "blockers": ["refunds_disabled"],
    }
    assert "test-secret" not in response.text
