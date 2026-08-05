"""YooKassa hosted checkout and merchant-managed recurring payment adapter."""

from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation
from typing import Any
from uuid import UUID

import httpx

from app.providers.payments.base import (
    PermanentProviderError,
    ProviderStateMismatch,
    UnknownProviderOutcome,
)
from app.providers.payments.gateway import AuthoritativePayment, CreateCheckout, HostedCheckout
from app.providers.payments.subscription_gateway import (
    CreateSubscriptionCheckout,
    HostedSubscriptionCheckout,
    InitialSubscriptionFailedFact,
    PaidSubscriptionFact,
    PastDueSubscriptionFact,
    RenewSubscription,
    SubscriptionProviderFact,
    SubscriptionStateFact,
    next_month_boundary,
)
from app.services.sensitive_content import (
    ContentPurpose,
    SensitiveContentCipher,
    SensitiveContentError,
)


class YooKassaGateway:
    def __init__(
        self,
        shop_id: str,
        secret_key: str,
        timeout: float = 15,
        vat_code: int = 1,
        payment_method_cipher: SensitiveContentCipher | None = None,
    ) -> None:
        self._auth = (shop_id, secret_key)
        self._timeout = timeout
        self._vat_code = vat_code
        self._payment_method_cipher = payment_method_cipher

    async def create_checkout(self, request: CreateCheckout) -> HostedCheckout:
        payload: dict[str, object] = {
            "amount": _amount(request.amount_minor),
            "capture": True,
            "confirmation": {"type": "redirect", "return_url": request.success_url},
            "description": f"HeartSignal: {request.product_code}"[:128],
            "metadata": {"order_id": request.order_id, "product_version": request.product_version},
        }
        receipt = self._receipt(request.product_code, request.amount_minor, request.receipt_contact)
        if receipt is not None:
            payload["receipt"] = receipt
        value, request_id = await self._post_payment(payload, request.idempotency_key)
        try:
            return HostedCheckout(
                str(value["id"]),
                str(value["confirmation"]["confirmation_url"]),
                str(value["status"]),
                request_id=request_id,
                live_mode=not bool(value.get("test", False)),
            )
        except (KeyError, TypeError) as exc:
            raise PermanentProviderError("malformed_checkout") from exc

    async def fetch_payment(self, checkout_id: str) -> AuthoritativePayment:
        value = await self._get_payment(checkout_id)
        amount = _mapping(value.get("amount"))
        metadata = _mapping(value.get("metadata"))
        status = str(value.get("status", "unknown"))
        try:
            amount_minor = parse_minor_amount(amount.get("value"))
        except ValueError as exc:
            raise PermanentProviderError("malformed_amount") from exc
        return AuthoritativePayment(
            checkout_id=str(value.get("id", "")),
            payment_id=str(value.get("id", "")),
            status=status,
            amount_minor=amount_minor,
            currency=str(amount.get("currency", "")),
            order_id=str(metadata.get("order_id", "")),
            mode=str(metadata.get("subscription_kind", "payment")),
            paid=bool(value.get("paid")) and status == "succeeded",
            live_mode=not bool(value.get("test", False)),
            provider_status=status,
        )

    async def create_subscription_checkout(
        self, request: CreateSubscriptionCheckout
    ) -> HostedSubscriptionCheckout:
        metadata = _subscription_metadata(
            user_id=request.user_id,
            order_id=request.order_id,
            product_code=request.product_code,
            product_version=request.product_version,
            market=request.market,
            currency=request.currency,
            amount_minor=request.amount_minor,
            credits=request.credits,
            price_reference=request.price_reference,
            consent_version=request.consent_version,
            subscription_kind="initial",
        )
        payload: dict[str, object] = {
            "amount": _amount(request.amount_minor),
            "capture": True,
            "save_payment_method": True,
            "confirmation": {"type": "redirect", "return_url": request.success_url},
            "description": f"HeartSignal: {request.product_code}"[:128],
            "metadata": metadata,
        }
        receipt = self._receipt(request.product_code, request.amount_minor, request.receipt_contact)
        if receipt is not None:
            payload["receipt"] = receipt
        value, _ = await self._post_payment(payload, request.idempotency_key)
        try:
            return HostedSubscriptionCheckout(
                checkout_id=str(value["id"]),
                url=str(value["confirmation"]["confirmation_url"]),
                status=str(value["status"]),
                live_mode=not bool(value.get("test", False)),
            )
        except (KeyError, TypeError) as exc:
            raise PermanentProviderError("malformed_subscription_checkout") from exc

    async def renew_subscription(self, request: RenewSubscription) -> SubscriptionProviderFact:
        payment_method_id = self._decrypt_payment_method(request.encrypted_payment_method)
        metadata = _subscription_metadata(
            user_id=request.user_id,
            order_id=None,
            product_code=request.product_code,
            product_version=request.product_version,
            market=request.market,
            currency=request.currency,
            amount_minor=request.amount_minor,
            credits=request.credits,
            price_reference=request.price_reference,
            consent_version=request.consent_version,
            subscription_kind="renewal",
            provider_subscription_id=request.provider_subscription_id,
            internal_subscription_id=request.subscription_id,
            period_start=request.period_start,
            period_end=request.period_end,
        )
        payload: dict[str, object] = {
            "amount": _amount(request.amount_minor),
            "capture": True,
            "payment_method_id": payment_method_id,
            "description": f"HeartSignal: {request.product_code}"[:128],
            "metadata": metadata,
        }
        receipt = self._receipt(request.product_code, request.amount_minor, request.receipt_contact)
        if receipt is not None:
            payload["receipt"] = receipt
        value, _ = await self._post_payment(payload, request.idempotency_key)
        status = str(value.get("status", "unknown"))
        if status not in {"succeeded", "canceled"}:
            raise UnknownProviderOutcome
        return self._subscription_fact(value)

    async def fetch_subscription_event(
        self, event_type: str, object_id: str
    ) -> SubscriptionProviderFact:
        del event_type
        value = await self._get_payment(object_id)
        status = str(value.get("status", "unknown"))
        if status not in {"succeeded", "canceled"}:
            raise UnknownProviderOutcome
        return self._subscription_fact(value)

    async def fetch_subscription(self, subscription_id: str) -> SubscriptionProviderFact:
        del subscription_id
        raise PermanentProviderError("provider_managed_subscription_unavailable")

    async def cancel_subscription(self, subscription_id: str) -> SubscriptionStateFact:
        del subscription_id
        raise PermanentProviderError("provider_managed_subscription_unavailable")

    async def resume_subscription(self, subscription_id: str) -> SubscriptionStateFact:
        del subscription_id
        raise PermanentProviderError("provider_managed_subscription_unavailable")

    async def _post_payment(
        self, payload: dict[str, object], idempotency_key: str
    ) -> tuple[dict[str, Any], str | None]:
        try:
            async with httpx.AsyncClient(timeout=self._timeout) as client:
                response = await client.post(
                    "https://api.yookassa.ru/v3/payments",
                    auth=self._auth,
                    headers={"Idempotence-Key": idempotency_key},
                    json=payload,
                )
        except (httpx.TimeoutException, httpx.NetworkError) as exc:
            raise UnknownProviderOutcome from exc
        if response.status_code >= 500:
            raise UnknownProviderOutcome
        if response.status_code >= 400:
            raise PermanentProviderError(f"http_{response.status_code}")
        value = response.json()
        if not isinstance(value, dict):
            raise PermanentProviderError("malformed_payment")
        return value, response.headers.get("X-Request-Id")

    async def _get_payment(self, payment_id: str) -> dict[str, Any]:
        try:
            async with httpx.AsyncClient(timeout=self._timeout) as client:
                response = await client.get(
                    f"https://api.yookassa.ru/v3/payments/{payment_id}", auth=self._auth
                )
        except (httpx.TimeoutException, httpx.NetworkError) as exc:
            raise UnknownProviderOutcome from exc
        if response.status_code >= 500:
            raise UnknownProviderOutcome
        if response.status_code >= 400:
            raise PermanentProviderError(f"http_{response.status_code}")
        value = response.json()
        if not isinstance(value, dict):
            raise PermanentProviderError("malformed_payment")
        return value

    def _subscription_fact(self, value: dict[str, Any]) -> SubscriptionProviderFact:
        metadata = _mapping(value.get("metadata"))
        kind = _required_text(metadata.get("subscription_kind"), "subscription_kind")
        if kind not in {"initial", "renewal"}:
            raise PermanentProviderError("not_subscription_payment")
        payment_id = _required_text(value.get("id"), "payment_id")
        user_id = _uuid(metadata.get("user_id"), "user_id")
        product_code = _required_text(metadata.get("product_code"), "product_code")
        product_version = _positive_int(metadata.get("product_version"), "product_version")
        market = _required_text(metadata.get("market"), "market")
        currency = _required_text(metadata.get("currency"), "currency").upper()
        amount_minor = _positive_int(metadata.get("amount_minor"), "amount_minor")
        credits = _positive_int(metadata.get("credits"), "credits")
        price_reference = _required_text(metadata.get("price_reference"), "price_reference")
        consent_version = _required_text(metadata.get("consent_version"), "consent_version")
        amount = _mapping(value.get("amount"))
        try:
            actual_amount = parse_minor_amount(amount.get("value"))
        except ValueError as exc:
            raise ProviderStateMismatch("subscription amount malformed") from exc
        actual_currency = str(amount.get("currency", "")).upper()
        if actual_amount != amount_minor or actual_currency != currency:
            raise ProviderStateMismatch("subscription commercial mismatch")

        initial_order_id: UUID | None = None
        encrypted_payment_method: bytes | None = None
        if kind == "initial":
            initial_order_id = _uuid(metadata.get("order_id"), "order_id")
            provider_subscription_id = f"yookassa:{initial_order_id}"
            created = _datetime(value.get("captured_at") or value.get("created_at"), "paid_at")
            period_start = created
            period_end = next_month_boundary(created)
        else:
            provider_subscription_id = _required_text(
                metadata.get("provider_subscription_id"), "provider_subscription_id"
            )
            period_start = _datetime(metadata.get("period_start"), "period_start")
            period_end = _datetime(metadata.get("period_end"), "period_end")

        status = str(value.get("status", "unknown"))
        if status == "canceled":
            if kind == "initial":
                assert initial_order_id is not None
                return InitialSubscriptionFailedFact(
                    user_id=user_id,
                    order_id=initial_order_id,
                    provider="yookassa",
                    provider_payment_id=payment_id,
                    provider_status=status,
                )
            return PastDueSubscriptionFact(
                provider="yookassa",
                provider_subscription_id=provider_subscription_id,
                provider_invoice_id=payment_id,
                product_code=product_code,
                product_version=product_version,
                currency=currency,
                amount_minor=amount_minor,
                credits=credits,
                period_start=period_start,
                period_end=period_end,
            )
        if status != "succeeded" or not bool(value.get("paid")):
            raise UnknownProviderOutcome
        paid_at = _datetime(value.get("captured_at") or value.get("created_at"), "paid_at")
        if kind == "initial":
            payment_method = _mapping(value.get("payment_method"))
            if not bool(payment_method.get("saved")):
                raise ProviderStateMismatch("payment method was not saved")
            encrypted_payment_method = self._encrypt_payment_method(
                _required_text(payment_method.get("id"), "payment_method_id")
            )
        return PaidSubscriptionFact(
            user_id=user_id,
            initial_order_id=initial_order_id,
            provider="yookassa",
            provider_customer_id=f"yookassa:{user_id}",
            provider_subscription_id=provider_subscription_id,
            provider_invoice_id=payment_id,
            provider_payment_id=payment_id,
            product_code=product_code,
            product_version=product_version,
            market=market,
            currency=currency,
            amount_minor=amount_minor,
            credits=credits,
            price_reference=price_reference,
            period_start=period_start,
            period_end=period_end,
            paid_at=paid_at,
            consent_version=consent_version,
            live_mode=not bool(value.get("test", False)),
            encrypted_payment_method=encrypted_payment_method,
        )

    def _encrypt_payment_method(self, payment_method_id: str) -> bytes:
        if self._payment_method_cipher is None:
            raise PermanentProviderError("payment_method_cipher_missing")
        return self._payment_method_cipher.encrypt_json(
            ContentPurpose.PAYMENT_METHOD, {"id": payment_method_id}
        )

    def _decrypt_payment_method(self, value: bytes | None) -> str:
        if value is None or self._payment_method_cipher is None:
            raise PermanentProviderError("saved_payment_method_missing")
        try:
            decrypted = self._payment_method_cipher.decrypt_json(
                ContentPurpose.PAYMENT_METHOD, value
            )
        except SensitiveContentError as exc:
            raise PermanentProviderError("saved_payment_method_invalid") from exc
        if not isinstance(decrypted, dict):
            raise PermanentProviderError("saved_payment_method_invalid")
        return _required_text(decrypted.get("id"), "payment_method_id")

    def _receipt(
        self, product_code: str, amount_minor: int, receipt_contact: str | None
    ) -> dict[str, object] | None:
        if not receipt_contact:
            return None
        customer_key = "email" if "@" in receipt_contact else "phone"
        return {
            "customer": {customer_key: receipt_contact},
            "items": [
                {
                    "description": f"HeartSignal: {product_code}",
                    "quantity": "1.00",
                    "amount": _amount(amount_minor),
                    "vat_code": self._vat_code,
                    "payment_mode": "full_payment",
                    "payment_subject": "service",
                }
            ],
        }


def _subscription_metadata(
    *,
    user_id: UUID,
    order_id: UUID | None,
    product_code: str,
    product_version: int,
    market: str,
    currency: str,
    amount_minor: int,
    credits: int,
    price_reference: str,
    consent_version: str,
    subscription_kind: str,
    provider_subscription_id: str | None = None,
    internal_subscription_id: UUID | None = None,
    period_start: datetime | None = None,
    period_end: datetime | None = None,
) -> dict[str, str]:
    value = {
        "billing_mode": "subscription",
        "subscription_kind": subscription_kind,
        "user_id": str(user_id),
        "product_code": product_code,
        "product_version": str(product_version),
        "market": market,
        "currency": currency,
        "amount_minor": str(amount_minor),
        "credits": str(credits),
        "price_reference": price_reference,
        "consent_version": consent_version,
    }
    optional = {
        "order_id": order_id,
        "provider_subscription_id": provider_subscription_id,
        "internal_subscription_id": internal_subscription_id,
        "period_start": period_start.astimezone(UTC).isoformat() if period_start else None,
        "period_end": period_end.astimezone(UTC).isoformat() if period_end else None,
    }
    value.update({key: str(item) for key, item in optional.items() if item is not None})
    return value


def _amount(amount_minor: int) -> dict[str, str]:
    return {
        "value": format(Decimal(amount_minor) / Decimal(100), ".2f"),
        "currency": "RUB",
    }


def _mapping(value: object) -> dict[str, Any]:
    return dict(value) if isinstance(value, dict) else {}


def _required_text(value: object, name: str) -> str:
    result = str(value or "").strip()
    if not result:
        raise ProviderStateMismatch(f"missing {name}")
    return result


def _positive_int(value: object, name: str) -> int:
    try:
        result = int(_required_text(value, name))
    except ValueError as exc:
        raise ProviderStateMismatch(f"invalid {name}") from exc
    if result <= 0:
        raise ProviderStateMismatch(f"invalid {name}")
    return result


def _uuid(value: object, name: str) -> UUID:
    try:
        return UUID(_required_text(value, name))
    except ValueError as exc:
        raise ProviderStateMismatch(f"invalid {name}") from exc


def _datetime(value: object, name: str) -> datetime:
    if isinstance(value, datetime):
        result = value
    elif isinstance(value, str):
        try:
            result = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError as exc:
            raise ProviderStateMismatch(f"invalid {name}") from exc
    else:
        raise ProviderStateMismatch(f"missing {name}")
    if result.tzinfo is None or result.utcoffset() is None:
        raise ProviderStateMismatch(f"invalid {name}")
    return result.astimezone(UTC)


def parse_minor_amount(value: object) -> int:
    """Parse a non-negative provider decimal with exactly zero-to-two fractional digits."""
    if not isinstance(value, str) or not value:
        raise ValueError("amount must be a decimal string")
    try:
        amount = Decimal(value)
    except InvalidOperation as exc:
        raise ValueError("malformed amount") from exc
    exponent = amount.as_tuple().exponent
    if not amount.is_finite() or amount < 0 or not isinstance(exponent, int) or exponent < -2:
        raise ValueError("invalid amount precision")
    minor = amount * 100
    if minor != minor.to_integral_value():
        raise ValueError("invalid amount precision")
    return int(minor)
