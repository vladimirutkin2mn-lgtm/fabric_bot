"""YooKassa full and supported partial monetary refund adapter."""

from decimal import Decimal, InvalidOperation
from typing import Any

import httpx

from app.providers.payments.base import PermanentProviderError, UnknownProviderOutcome
from app.providers.payments.refund_gateway import (
    AuthoritativeRefund,
    CreateRefund,
    RefundCapabilities,
)


class YooKassaRefundGateway:
    def __init__(
        self,
        shop_id: str,
        secret_key: str,
        timeout: float = 15,
        partial_refunds: bool = True,
    ) -> None:
        self._auth = (shop_id, secret_key)
        self._timeout = timeout
        self._capabilities = RefundCapabilities(partial_refunds=partial_refunds)

    @property
    def refund_capabilities(self) -> RefundCapabilities:
        return self._capabilities

    async def create_refund(self, request: CreateRefund) -> AuthoritativeRefund:
        if len(request.idempotency_key) > 64:
            raise PermanentProviderError("refund_idempotency_key_too_long")
        payload: dict[str, object] = {
            "payment_id": request.provider_payment_id,
            "amount": {
                "value": format(Decimal(request.amount_minor) / Decimal(100), ".2f"),
                "currency": request.currency,
            },
            "description": "HeartSignal refund"[:128],
            "metadata": {"refund_request_id": str(request.refund_request_id)},
        }
        try:
            async with httpx.AsyncClient(timeout=self._timeout) as client:
                response = await client.post(
                    "https://api.yookassa.ru/v3/refunds",
                    auth=self._auth,
                    headers={"Idempotence-Key": request.idempotency_key},
                    json=payload,
                )
        except (httpx.TimeoutException, httpx.NetworkError) as exc:
            raise UnknownProviderOutcome from exc
        return self._response(response)

    async def fetch_refund(self, refund_id: str) -> AuthoritativeRefund:
        try:
            async with httpx.AsyncClient(timeout=self._timeout) as client:
                response = await client.get(
                    f"https://api.yookassa.ru/v3/refunds/{refund_id}",
                    auth=self._auth,
                )
        except (httpx.TimeoutException, httpx.NetworkError) as exc:
            raise UnknownProviderOutcome from exc
        return self._response(response)

    @staticmethod
    def _response(response: httpx.Response) -> AuthoritativeRefund:
        if response.status_code >= 500:
            raise UnknownProviderOutcome
        if response.status_code >= 400:
            raise PermanentProviderError(f"http_{response.status_code}")
        value = response.json()
        if not isinstance(value, dict):
            raise PermanentProviderError("malformed_refund")
        status_value = str(value.get("status", "unknown"))
        if status_value == "succeeded":
            status = "succeeded"
        elif status_value == "canceled":
            status = "failed"
        elif status_value == "pending":
            status = "pending"
        else:
            raise PermanentProviderError("unknown_refund_status")
        amount = value.get("amount")
        if not isinstance(amount, dict):
            raise PermanentProviderError("malformed_refund_amount")
        cancellation = value.get("cancellation_details")
        failure_code = (
            str(cancellation.get("reason"))
            if isinstance(cancellation, dict) and cancellation.get("reason")
            else None
        )
        try:
            amount_minor = _parse_minor(amount.get("value"))
        except ValueError as exc:
            raise PermanentProviderError("malformed_refund_amount") from exc
        provider_refund_id = str(value.get("id", "")).strip()
        payment_id = str(value.get("payment_id", "")).strip()
        currency = str(amount.get("currency", "")).upper()
        if not provider_refund_id or not payment_id or len(currency) != 3:
            raise PermanentProviderError("malformed_refund")
        return AuthoritativeRefund(
            provider="yookassa",
            provider_refund_id=provider_refund_id,
            provider_payment_id=payment_id,
            status=status,
            amount_minor=amount_minor,
            currency=currency,
            provider_status=status_value,
            failure_code=failure_code,
            live_mode=(not bool(value.get("test")) if "test" in value else None),
        )


def _parse_minor(value: Any) -> int:
    try:
        amount = Decimal(str(value))
    except (InvalidOperation, ValueError) as exc:
        raise ValueError("invalid amount") from exc
    minor = amount * 100
    if amount <= 0 or minor != minor.to_integral_value():
        raise ValueError("invalid amount")
    return int(minor)
