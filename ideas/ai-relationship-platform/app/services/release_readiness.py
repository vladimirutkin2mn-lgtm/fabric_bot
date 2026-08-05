"""Fail-closed staging acceptance and limited-production readiness service."""

import os
import re
from datetime import UTC, datetime
from enum import StrEnum

from pydantic import BaseModel, Field
from sqlalchemy import exists, func, select, text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.config import Settings
from app.db.models import (
    BillingJob,
    BillingOutboxEvent,
    CreditReservation,
    CreditTransaction,
    PaymentOrder,
    RefundRequest,
)
from app.db.release_gates import ReleaseGateAttestation

_EVIDENCE_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:/#-]*$")
_CODE_SHA_PATTERN = re.compile(r"^[0-9a-f]{7,64}$")
_CHECKLIST_PATTERN = re.compile(r"^[a-z0-9][a-z0-9._-]{0,63}$")


class ReleaseGateName(StrEnum):
    STRIPE_SUBSCRIPTION = "stripe_subscription_sandbox"
    YOOKASSA_SUBSCRIPTION = "yookassa_subscription_sandbox"
    STRIPE_REFUND = "stripe_refund_sandbox"
    YOOKASSA_REFUND = "yookassa_refund_sandbox"
    OPENAI_FOLLOWUP = "openai_followup_staging"


class ReleaseGateResult(StrEnum):
    PASSED = "passed"
    FAILED = "failed"


class ReleaseGateState(StrEnum):
    MISSING = "missing"
    FAILED = "failed"
    STALE = "stale"
    PASSED = "passed"


class ReleaseGateAttestationRequest(BaseModel):
    status: ReleaseGateResult
    evidence_ref: str = Field(min_length=1, max_length=512)


class ReleaseGateView(BaseModel):
    gate_name: ReleaseGateName
    state: ReleaseGateState
    latest_result: ReleaseGateResult | None = None
    evidence_ref: str | None = None
    attested_at: datetime | None = None
    current_code: bool = False
    current_schema: bool = False
    current_checklist: bool = False
    configuration_blockers: list[str] = Field(default_factory=list)


class ReleaseReadiness(BaseModel):
    generated_at: datetime
    app_env: str
    code_sha: str | None
    schema_revision: str | None
    checklist_version: str
    gates: list[ReleaseGateView]
    financial_blockers: dict[str, int]
    blockers: list[str]
    ready_for_limited_production: bool


class ReleaseGateError(RuntimeError):
    def __init__(self, code: str, blockers: list[str] | None = None) -> None:
        super().__init__(code)
        self.code = code
        self.blockers = blockers or []


class ReleaseReadinessService:
    """Record exact staging evidence and compute a non-bypassable release snapshot."""

    def __init__(
        self,
        sessions: async_sessionmaker[AsyncSession],
        settings: Settings,
        *,
        code_sha: str | None = None,
        checklist_version: str | None = None,
    ) -> None:
        self._sessions = sessions
        self._settings = settings
        self._code_sha = (code_sha if code_sha is not None else os.getenv("RELEASE_CODE_SHA", "")).strip()
        self._checklist_version = (
            checklist_version
            if checklist_version is not None
            else os.getenv("RELEASE_CHECKLIST_VERSION", "m5-live-v1")
        ).strip()

    async def snapshot(self) -> ReleaseReadiness:
        async with self._sessions() as session:
            schema_revision = await self._schema_revision(session)
            latest = await self._latest_attestations(session)
            financial = await self._financial_blockers(session)

        gates = [
            self._gate_view(name, latest.get(name), schema_revision) for name in ReleaseGateName
        ]
        blockers = self._identity_blockers(schema_revision)
        for gate in gates:
            blockers.extend(f"{gate.gate_name}:{item}" for item in gate.configuration_blockers)
            if gate.state is not ReleaseGateState.PASSED:
                blockers.append(f"{gate.gate_name}:{gate.state}")
        blockers.extend(name for name, count in financial.items() if count > 0)
        unique_blockers = sorted(set(blockers))
        return ReleaseReadiness(
            generated_at=datetime.now(UTC),
            app_env=self._settings.app_env,
            code_sha=self._code_sha or None,
            schema_revision=schema_revision,
            checklist_version=self._checklist_version,
            gates=gates,
            financial_blockers=financial,
            blockers=unique_blockers,
            ready_for_limited_production=not unique_blockers,
        )

    async def attest(
        self,
        gate_name: ReleaseGateName,
        request: ReleaseGateAttestationRequest,
    ) -> ReleaseReadiness:
        if not _EVIDENCE_PATTERN.fullmatch(request.evidence_ref):
            raise ReleaseGateError("invalid_evidence_ref")
        if self._settings.app_env != "staging":
            raise ReleaseGateError("staging_only")
        if not _CODE_SHA_PATTERN.fullmatch(self._code_sha):
            raise ReleaseGateError("release_code_sha_invalid")
        if not _CHECKLIST_PATTERN.fullmatch(self._checklist_version):
            raise ReleaseGateError("release_checklist_version_invalid")

        async with self._sessions.begin() as session:
            schema_revision = await self._schema_revision(session)
            if schema_revision is None:
                raise ReleaseGateError("schema_revision_missing")
            configuration = self._configuration_blockers(gate_name)
            if request.status is ReleaseGateResult.PASSED and configuration:
                raise ReleaseGateError("gate_configuration_incomplete", configuration)
            session.add(
                ReleaseGateAttestation(
                    gate_name=gate_name.value,
                    status=request.status.value,
                    checklist_version=self._checklist_version,
                    app_env=self._settings.app_env,
                    code_sha=self._code_sha,
                    schema_revision=schema_revision,
                    evidence_ref=request.evidence_ref,
                )
            )
        return await self.snapshot()

    def _identity_blockers(self, schema_revision: str | None) -> list[str]:
        blockers: list[str] = []
        if self._settings.app_env != "staging":
            blockers.append("environment_not_staging")
        if not _CODE_SHA_PATTERN.fullmatch(self._code_sha):
            blockers.append("release_code_sha_invalid")
        if not _CHECKLIST_PATTERN.fullmatch(self._checklist_version):
            blockers.append("release_checklist_version_invalid")
        if schema_revision is None:
            blockers.append("schema_revision_missing")
        return blockers

    async def _latest_attestations(
        self, session: AsyncSession
    ) -> dict[ReleaseGateName, ReleaseGateAttestation]:
        rows = (
            await session.scalars(
                select(ReleaseGateAttestation).order_by(
                    ReleaseGateAttestation.gate_name,
                    ReleaseGateAttestation.attested_at.desc(),
                    ReleaseGateAttestation.id.desc(),
                )
            )
        ).all()
        latest: dict[ReleaseGateName, ReleaseGateAttestation] = {}
        for row in rows:
            try:
                name = ReleaseGateName(row.gate_name)
            except ValueError:
                continue
            latest.setdefault(name, row)
        return latest

    def _gate_view(
        self,
        gate_name: ReleaseGateName,
        row: ReleaseGateAttestation | None,
        schema_revision: str | None,
    ) -> ReleaseGateView:
        configuration = self._configuration_blockers(gate_name)
        if row is None:
            state = ReleaseGateState.MISSING
            current_code = current_schema = current_checklist = False
        else:
            current_code = row.code_sha == self._code_sha
            current_schema = row.schema_revision == schema_revision
            current_checklist = row.checklist_version == self._checklist_version
            if row.status == ReleaseGateResult.FAILED.value:
                state = ReleaseGateState.FAILED
            elif current_code and current_schema and current_checklist:
                state = ReleaseGateState.PASSED
            else:
                state = ReleaseGateState.STALE
        return ReleaseGateView(
            gate_name=gate_name,
            state=state,
            latest_result=(ReleaseGateResult(row.status) if row is not None else None),
            evidence_ref=(row.evidence_ref if row is not None else None),
            attested_at=(row.attested_at if row is not None else None),
            current_code=current_code,
            current_schema=current_schema,
            current_checklist=current_checklist,
            configuration_blockers=configuration,
        )

    def _configuration_blockers(self, gate_name: ReleaseGateName) -> list[str]:
        settings = self._settings
        blockers: list[str] = []
        if gate_name is not ReleaseGateName.OPENAI_FOLLOWUP:
            if not settings.billing_enabled:
                blockers.append("billing_disabled")
            if not settings.payment_public_base_url.startswith("https://"):
                blockers.append("public_https_missing")
        if gate_name in {
            ReleaseGateName.STRIPE_SUBSCRIPTION,
            ReleaseGateName.STRIPE_REFUND,
        }:
            key = settings.stripe_secret_key.get_secret_value()
            if not settings.stripe_enabled:
                blockers.append("stripe_disabled")
            if not key.startswith(("sk_test_", "rk_test_")):
                blockers.append("stripe_test_credentials_required")
            if not settings.stripe_webhook_secret.get_secret_value():
                blockers.append("stripe_webhook_secret_missing")
        if gate_name is ReleaseGateName.STRIPE_SUBSCRIPTION:
            if not settings.subscriptions_enabled:
                blockers.append("subscriptions_disabled")
            configured_offer = any(
                bool(price) and amount is not None
                for price, amount in (
                    (
                        settings.stripe_price_subscription_monthly_eur,
                        settings.stripe_amount_subscription_monthly_eur_minor,
                    ),
                    (
                        settings.stripe_price_subscription_monthly_usd,
                        settings.stripe_amount_subscription_monthly_usd_minor,
                    ),
                )
            )
            if not configured_offer:
                blockers.append("stripe_subscription_offer_missing")
        if gate_name in {
            ReleaseGateName.YOOKASSA_SUBSCRIPTION,
            ReleaseGateName.YOOKASSA_REFUND,
        }:
            if not settings.yookassa_enabled:
                blockers.append("yookassa_disabled")
            if not (
                settings.yookassa_shop_id.get_secret_value()
                and settings.yookassa_secret_key.get_secret_value()
            ):
                blockers.append("yookassa_test_credentials_missing")
            if not settings.yookassa_webhook_ip_allowlist.strip():
                blockers.append("yookassa_webhook_allowlist_missing")
        if gate_name is ReleaseGateName.YOOKASSA_SUBSCRIPTION:
            if not settings.subscriptions_enabled:
                blockers.append("subscriptions_disabled")
            if not settings.yookassa_recurring_enabled:
                blockers.append("yookassa_recurring_disabled")
        if gate_name in {
            ReleaseGateName.STRIPE_REFUND,
            ReleaseGateName.YOOKASSA_REFUND,
        } and not settings.refunds_enabled:
            blockers.append("refunds_disabled")
        if gate_name is ReleaseGateName.OPENAI_FOLLOWUP:
            if settings.llm_provider != "openai":
                blockers.append("openai_provider_required")
            if not settings.openai_api_key.get_secret_value():
                blockers.append("openai_api_key_missing")
            if not settings.llm_model or settings.llm_model == "stub":
                blockers.append("openai_model_missing")
        return sorted(set(blockers))

    @staticmethod
    async def _schema_revision(session: AsyncSession) -> str | None:
        try:
            result = await session.execute(
                text("SELECT version_num FROM alembic_version ORDER BY version_num")
            )
        except Exception:
            return None
        values = [str(row[0]) for row in result]
        return ",".join(values) or None

    @staticmethod
    async def _financial_blockers(session: AsyncSession) -> dict[str, int]:
        job_manual = await session.scalar(
            select(func.count()).select_from(BillingJob).where(BillingJob.status == "manual_review")
        )
        job_failed = await session.scalar(
            select(func.count()).select_from(BillingJob).where(BillingJob.status == "failed")
        )
        outbox_manual = await session.scalar(
            select(func.count())
            .select_from(BillingOutboxEvent)
            .where(BillingOutboxEvent.status == "manual_review")
        )
        outbox_failed = await session.scalar(
            select(func.count())
            .select_from(BillingOutboxEvent)
            .where(BillingOutboxEvent.status == "failed")
        )
        order_manual = await session.scalar(
            select(func.count())
            .select_from(PaymentOrder)
            .where(PaymentOrder.status == "manual_review")
        )
        refund_manual = await session.scalar(
            select(func.count())
            .select_from(RefundRequest)
            .where(RefundRequest.status == "manual_review")
        )
        missing_ledger = await session.scalar(
            select(func.count())
            .select_from(RefundRequest)
            .where(
                RefundRequest.status == "succeeded",
                ~exists(
                    select(CreditTransaction.id).where(
                        CreditTransaction.refund_request_id == RefundRequest.id,
                        CreditTransaction.type == "purchase_refund",
                    )
                ),
            )
        )
        orphan_ledger = await session.scalar(
            select(func.count())
            .select_from(CreditTransaction)
            .where(
                CreditTransaction.type == "purchase_refund",
                ~exists(
                    select(RefundRequest.id).where(
                        RefundRequest.id == CreditTransaction.refund_request_id,
                        RefundRequest.status == "succeeded",
                    )
                ),
            )
        )
        reservation_mismatch = await session.scalar(
            select(func.count())
            .select_from(CreditReservation)
            .join(RefundRequest, RefundRequest.id == CreditReservation.refund_request_id)
            .where(
                (
                    (RefundRequest.status == "succeeded")
                    & (CreditReservation.status != "consumed")
                )
                | (
                    (RefundRequest.status == "failed")
                    & (CreditReservation.status != "released")
                )
            )
        )
        return {
            "billing_jobs_manual_review": int(job_manual or 0),
            "billing_jobs_failed": int(job_failed or 0),
            "billing_outbox_manual_review": int(outbox_manual or 0),
            "billing_outbox_failed": int(outbox_failed or 0),
            "payment_orders_manual_review": int(order_manual or 0),
            "refunds_manual_review": int(refund_manual or 0),
            "succeeded_refunds_without_ledger": int(missing_ledger or 0),
            "refund_ledger_without_success": int(orphan_ledger or 0),
            "refund_reservation_state_mismatch": int(reservation_mismatch or 0),
        }
