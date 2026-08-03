"""YooKassa one-stage redirect adapter using its documented REST API."""

import httpx

from app.providers.payments.base import PermanentProviderError, UnknownProviderOutcome
from app.providers.payments.gateway import AuthoritativePayment, CreateCheckout, HostedCheckout


class YooKassaGateway:
    def __init__(
        self, shop_id: str, secret_key: str, timeout: float = 15, vat_code: int = 1
    ) -> None:
        self._auth = (shop_id, secret_key)
        self._timeout = timeout
        self._vat_code = vat_code

    async def create_checkout(self, request: CreateCheckout) -> HostedCheckout:
        payload: dict[str, object] = {
            "amount": {"value": f"{request.amount_minor / 100:.2f}", "currency": "RUB"},
            "capture": True,
            "confirmation": {"type": "redirect", "return_url": request.success_url},
            "description": f"HeartSignal: {request.product_code}"[:128],
            "metadata": {"order_id": request.order_id, "product_version": request.product_version},
        }
        if request.receipt_contact:
            customer_key = "email" if "@" in request.receipt_contact else "phone"
            payload["receipt"] = {
                "customer": {customer_key: request.receipt_contact},
                "items": [
                    {
                        "description": f"HeartSignal: {request.product_code}",
                        "quantity": "1.00",
                        "amount": payload["amount"],
                        "vat_code": self._vat_code,
                        "payment_mode": "full_payment",
                        "payment_subject": "service",
                    }
                ],
            }
        try:
            async with httpx.AsyncClient(timeout=self._timeout) as client:
                response = await client.post(
                    "https://api.yookassa.ru/v3/payments",
                    auth=self._auth,
                    headers={"Idempotence-Key": request.idempotency_key},
                    json=payload,
                )
        except (httpx.TimeoutException, httpx.NetworkError) as exc:
            raise UnknownProviderOutcome from exc
        if response.status_code >= 500:
            raise UnknownProviderOutcome
        if response.status_code >= 400:
            raise PermanentProviderError(f"http_{response.status_code}")
        value = response.json()
        try:
            return HostedCheckout(
                str(value["id"]),
                str(value["confirmation"]["confirmation_url"]),
                str(value["status"]),
                request_id=response.headers.get("X-Request-Id"),
                live_mode=not bool(value.get("test", False)),
            )
        except (KeyError, TypeError) as exc:
            raise PermanentProviderError("malformed_checkout") from exc

    async def fetch_payment(self, checkout_id: str) -> AuthoritativePayment:
        try:
            async with httpx.AsyncClient(timeout=self._timeout) as client:
                response = await client.get(
                    f"https://api.yookassa.ru/v3/payments/{checkout_id}", auth=self._auth
                )
        except (httpx.TimeoutException, httpx.NetworkError) as exc:
            raise UnknownProviderOutcome from exc
        if response.status_code >= 500:
            raise UnknownProviderOutcome
        if response.status_code >= 400:
            raise PermanentProviderError(f"http_{response.status_code}")
        value = response.json()
        amount = value.get("amount", {})
        metadata = value.get("metadata", {})
        status = str(value.get("status", "unknown"))
        return AuthoritativePayment(
            checkout_id=str(value.get("id", "")),
            payment_id=str(value.get("id", "")),
            status=status,
            amount_minor=round(float(amount.get("value", "0")) * 100),
            currency=str(amount.get("currency", "")),
            order_id=str(metadata.get("order_id", "")),
            paid=bool(value.get("paid")) and status == "succeeded",
            live_mode=not bool(value.get("test", False)),
            provider_status=status,
        )
