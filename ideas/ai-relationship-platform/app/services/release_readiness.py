"""Fail-closed staging acceptance and limited-production readiness service."""

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

    def validate_evidence(self) -> None:
        if not _EVIDENCE_PATTERN.fullmatch(self.evidence_ref):
            raise ValueError("evidence_ref must be an opaque ASCII reference without query data")


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

    def __init__(self, sessions: async_sessionmaker[AsyncSession], settings: Settings) -> None:
        self._sessions = sessions
        self._settings = settings

    async def snapshot(self) -> ReleaseReadiness:
        async with self._sessions() as session:
            schema_revision = await self._schema_revision(session)
            latest = await self._latest_attestations(session)
            financial = await self._financial_blockers(session)

        gates = [
            self._gate_view(name, latest.get(name), schema_revision) for name in ReleaseGateName
        ]
        blockers: list[str] = []
        if self._settings.app_env != "staging":
            blockers.append("environment_not_staging")
        if not self._settings.release_code_sha:
            blockers.append("release_code_sha_missing")
        if schema_revision is None:
            blockers.append("schema_revision_missing")
        for gate in gates:
            blockers.extend(f"{gate.gate_name}:{item}" for item in gate.configuration_blockers)
            if gate.state is not ReleaseGateState.PASSED:
                blockers.append(f"{gate.gate_name}:{gate.state}")
        blockers.extend(name for name, count in financial.items() if count > 0)
        unique_blockers = sorted(set(blockers))
        return ReleaseReadiness(
            generated_at=datetime.now(UTC),
            app_env=self._settings.app_env,
            code_sha=self._settings.release_code_sha or None,
            schema_revision=schema_revision,
            checklist_version=self._settings.release_checklist_version,
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
        try:
            request.validate_evidence()
        except ValueError as exc:
            raise ReleaseGateError("invalid_evidence_ref") from exc
        if self._settings.app_env != "staging":
            raise ReleaseGateError("staging_only")
        if not self._settings.release_code_sha:
            raise ReleaseGateError("release_code_sha_missing")

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
                    checklist_version=self._settings.release_checklist_version,
                    app_env=self._settings.app_env,
                    code_sha=self._settings.release_code_sha,
                    schema_revision=schema_revision,
                    evidence_ref=request.evidence_ref,
                )
            )
        return await self.snapshot()

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
            current_code = row.code_sha == self._settings.release_code_sha
            current_schema = row.schema_revision == schema_revision
            current_checklist = row.checklist_version == self._settings.release_checklist_version
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
        if not settings.billing_enabled and gate_name is not ReleaseGateName.OPENAI_FOLLOWUP:
            blockers.append("billing_disabled")
        if not settings.payment_public_base_url.startswith("https://") and gate_name is not ReleaseGateName.OPENAI_FOLLOWUP:
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
                price and amount is not None
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
            if not settings.yookassa_shop_id.get_secret_value() or not settings.yookassa_secret_key.get_secret_value():
                blockers.append("yookassa_test_credentials_missing")
            if not settings.yookassa_webhook_ip_allowlist.strip():
                blockers.append("yookassa_webhook_allowlist_missing")
        if gate_name is ReleaseGateName.YOOKASSA_SUBSCRIPTION:
            if not settings.subscriptions_enabled:
                blockers.append("subscriptions_disabled")
            if not settings.yookassa_recurring_enabled:
                blockers.append("yookassa_recurring_disabled")
        if gate_name in {ReleaseGateName.STRIPE_REFUND, ReleaseGateName.YOOKASSA_REFUND}:
            if not settings.refunds_enabled:
                blockers.append("refunds_disabled")
        if gate_name is ReleaseGateName.OPENAI_FOLLOWUP:
            if settings.llm_provider != "openai":
                blockers.append("openai_provider_required")
            if not settings.openai_api_key.get_secret_value():
                blockers.append("openai_api_key_missing")
            if not settings.llm_model or settings.llm_model == "stub":
                blockers.append("openai_model_missing")
        return sorted(set(blockers))

    async def _schema_revision(self, session: AsyncSession) -> str | None:
        try:
            values = list(
                await session.scalars(text("SELECT version_num FROM alembic_version ORDER BY version_num"))
            )
        except Exception:
            return None
        return ",".join(str(value) for value in values) or None

    async def _financial_blockers(self, session: AsyncSession) -> dict[str, int]:
        counts = {
            "billing_jobs_manual_review": await self._status_count(
                session, BillingJob, "manual_review"
            ),
            "billing_jobs_failed": await self._status_count(session, BillingJob, "failed"),
            "billing_outbox_manual_review": await self._status_count(
                session, BillingOutboxEvent, "manual_review"
            ),
            "billing_outbox_failed": await self._status_count(
                session, BillingOutboxEvent, "failed"
            ),
            "payment_orders_manual_review": await self._status_count(
                session, PaymentOrder, "manual_review"
            ),
            "refunds_manual_review": await self._status_count(
                session, RefundRequest, "manual_review"
            ),
        }
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
        inconsistent_reservations = await session.scalar(
            select(func.count())
            .select_from(CreditReservation)
            .join(RefundRequest, RefundRequest.id == CreditReservation.refund_request_id)
            .where(
                ((RefundRequest.status == "succeeded") & (CreditReservation.status != "consumed"))
                | ((RefundRequest.status == "failed") & (CreditReservation.status != "released"))
            )
        )
        counts["succeeded_refunds_without_ledger"] = int(missing_ledger or 0)
        counts["refund_ledger_without_success"] = int(orphan_ledger or 0)
        counts["refund_reservation_state_mismatch"] = int(inconsistent_reservations or 0)
        return counts

    @staticmethod
    async def _status_count(session: AsyncSession, model: type[object], status: str) -> int:
        column = getattr(model, "status")
        value = await session.scalar(select(func.count()).select_from(model).where(column == status))
        return int(value or 0)
