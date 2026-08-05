"""Provider-neutral monetary refund contracts.

Provider SDK objects and raw responses never cross this boundary. A gateway may create
or retrieve a refund, but only the durable refund processor mutates credit reservations
or the append-only ledger.
"""

from dataclasses import dataclass
from typing import Protocol
from uuid import UUID


@dataclass(frozen=True)
class RefundCapabilities:
    partial_refunds: bool


@dataclass(frozen=True)
class CreateRefund:
    user_id: UUID
    refund_request_id: UUID
    provider_payment_id: str
    amount_minor: int
    currency: str
    reason: str
    idempotency_key: str


@dataclass(frozen=True)
class AuthoritativeRefund:
    provider: str
    provider_refund_id: str
    provider_payment_id: str
    status: str
    amount_minor: int
    currency: str
    provider_status: str
    failure_code: str | None = None
    live_mode: bool | None = None


class RefundGateway(Protocol):
    @property
    def refund_capabilities(self) -> RefundCapabilities: ...

    async def create_refund(self, request: CreateRefund) -> AuthoritativeRefund: ...

    async def fetch_refund(self, refund_id: str) -> AuthoritativeRefund: ...
