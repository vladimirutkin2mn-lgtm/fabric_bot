"""Stripe monetary refund adapter using PaymentIntent identity and idempotency."""

import asyncio
import importlib
from collections.abc import Mapping
from typing import Any

from app.providers.payments.base import PermanentProviderError, UnknownProviderOutcome
from app.providers.payments.refund_gateway import (
    AuthoritativeRefund,
    CreateRefund,
    RefundCapabilities,
)


class StripeRefundGateway:
    def __init__(self, api_key: str, timeout: float = 15) -> None:
        self._stripe = importlib.import_module("stripe")
        self._client = self._stripe.StripeClient(api_key, max_network_retries=0)
        self._timeout = timeout

    @property
    def refund_capabilities(self) -> RefundCapabilities:
        return RefundCapabilities(partial_refunds=True)

    async def create_refund(self, request: CreateRefund) -> AuthoritativeRefund:
        params: dict[str, object] = {
            "payment_intent": request.provider_payment_id,
            "amount": request.amount_minor,
            "reason": "requested_by_customer",
            "metadata": {"refund_request_id": str(request.refund_request_id)},
        }
        try:
            value = await asyncio.wait_for(
                asyncio.to_thread(
                    self._client.refunds.create,
                    params,
                    options={"idempotency_key": request.idempotency_key},
                ),
                self._timeout,
            )
        except (TimeoutError, self._stripe.APIConnectionError) as exc:
            raise UnknownProviderOutcome from exc
        except self._stripe.StripeError as exc:
            raise PermanentProviderError(type(exc).__name__) from exc
        return self._fact(value)

    async def fetch_refund(self, refund_id: str) -> AuthoritativeRefund:
        try:
            value = await asyncio.wait_for(
                asyncio.to_thread(self._client.refunds.retrieve, refund_id),
                self._timeout,
            )
        except (TimeoutError, self._stripe.APIConnectionError) as exc:
            raise UnknownProviderOutcome from exc
        except self._stripe.StripeError as exc:
            raise PermanentProviderError(type(exc).__name__) from exc
        return self._fact(value)

    @staticmethod
    def _fact(value: object) -> AuthoritativeRefund:
        provider_status = _required(value, "status")
        if provider_status == "succeeded":
            status = "succeeded"
        elif provider_status in {"failed", "canceled"}:
            status = "failed"
        elif provider_status in {"pending", "requires_action"}:
            status = "pending"
        else:
            raise PermanentProviderError("unknown_refund_status")
        payment_intent = _value(value, "payment_intent")
        payment_id = _value(payment_intent, "id") if payment_intent is not None else None
        if not payment_id and isinstance(payment_intent, str):
            payment_id = payment_intent
        amount = _value(value, "amount")
        currency = str(_value(value, "currency") or "").upper()
        if not isinstance(amount, int) or amount < 1 or len(currency) != 3:
            raise PermanentProviderError("malformed_refund")
        return AuthoritativeRefund(
            provider="stripe",
            provider_refund_id=_required(value, "id"),
            provider_payment_id=str(payment_id or ""),
            status=status,
            amount_minor=amount,
            currency=currency,
            provider_status=provider_status,
            failure_code=(
                str(_value(value, "failure_reason"))
                if _value(value, "failure_reason")
                else None
            ),
            live_mode=(
                bool(_value(value, "livemode"))
                if _value(value, "livemode") is not None
                else None
            ),
        )


def _value(value: object, name: str) -> Any:
    if isinstance(value, Mapping):
        return value.get(name)
    return getattr(value, name, None)


def _required(value: object, name: str) -> str:
    result = str(_value(value, name) or "").strip()
    if not result:
        raise PermanentProviderError(f"missing_{name}")
    return result
