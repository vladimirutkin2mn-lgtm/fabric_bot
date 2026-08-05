"""Stripe hosted Checkout and subscription adapter.

Vendor objects never leave this module. Only provider-authoritative, privacy-safe facts
cross into the billing services.
"""

import asyncio
import importlib
from collections.abc import Mapping
from datetime import UTC, datetime
from typing import Any
from uuid import UUID

from app.providers.payments.base import (
    PaymentPayloadError,
    PaymentSignatureError,
    PermanentProviderError,
    ProviderStateMismatch,
    UnknownProviderOutcome,
)
from app.providers.payments.gateway import AuthoritativePayment, CreateCheckout, HostedCheckout
from app.providers.payments.subscription_gateway import (
    CreateSubscriptionCheckout,
    HostedSubscriptionCheckout,
    PaidSubscriptionFact,
    PastDueSubscriptionFact,
    SubscriptionProviderFact,
    SubscriptionStateFact,
)


class StripeGateway:
    def __init__(self, api_key: str, webhook_secret: str, timeout: float = 15) -> None:
        self._stripe = importlib.import_module("stripe")
        self._client = self._stripe.StripeClient(api_key, max_network_retries=0)
        self._webhook_secret = webhook_secret
        self._timeout = timeout

    def verify_webhook(self, payload: bytes, signature: str) -> Mapping[str, object]:
        try:
            event = self._stripe.Webhook.construct_event(payload, signature, self._webhook_secret)
        except self._stripe.SignatureVerificationError as exc:
            raise PaymentSignatureError from exc
        except (ValueError, TypeError) as exc:
            raise PaymentPayloadError from exc
        return dict(event)

    async def create_checkout(self, request: CreateCheckout) -> HostedCheckout:
        try:
            result = await asyncio.wait_for(
                asyncio.to_thread(
                    self._client.checkout.sessions.create,
                    {
                        "mode": "payment",
                        "line_items": [{"price": request.price_reference, "quantity": 1}],
                        "success_url": request.success_url,
                        "cancel_url": request.cancel_url,
                        "client_reference_id": request.order_id,
                        "metadata": {
                            "order_id": request.order_id,
                            "product_version": str(request.product_version),
                        },
                    },
                    options={"idempotency_key": request.idempotency_key},
                ),
                self._timeout,
            )
        except (TimeoutError, self._stripe.APIConnectionError) as exc:
            raise UnknownProviderOutcome from exc
        except self._stripe.StripeError as exc:
            raise PermanentProviderError(type(exc).__name__) from exc
        if not result.id or not result.url:
            raise PermanentProviderError("malformed_checkout")
        expires = datetime.fromtimestamp(result.expires_at, UTC) if result.expires_at else None
        payment_id = str(result.payment_intent) if result.payment_intent else None
        return HostedCheckout(
            result.id,
            result.url,
            str(result.status),
            payment_id,
            expires_at=expires,
            live_mode=bool(result.livemode),
        )

    async def fetch_payment(self, checkout_id: str) -> AuthoritativePayment:
        try:
            value = await asyncio.wait_for(
                asyncio.to_thread(
                    self._client.checkout.sessions.retrieve,
                    checkout_id,
                    params={"expand": ["payment_intent"]},
                ),
                self._timeout,
            )
        except (TimeoutError, self._stripe.APIConnectionError) as exc:
            raise UnknownProviderOutcome from exc
        except self._stripe.StripeError as exc:
            raise PermanentProviderError(type(exc).__name__) from exc
        metadata = dict(value.metadata or {})
        intent = value.payment_intent
        payment_id = intent.id if hasattr(intent, "id") else str(intent or "")
        return AuthoritativePayment(
            checkout_id=value.id,
            payment_id=payment_id or value.id,
            status="succeeded" if value.payment_status == "paid" else str(value.status),
            amount_minor=int(value.amount_total or 0),
            currency=str(value.currency or "").upper(),
            order_id=str(metadata.get("order_id") or value.client_reference_id or ""),
            mode=str(value.mode),
            paid=value.payment_status == "paid",
            live_mode=bool(value.livemode),
            provider_status=str(value.payment_status),
        )

    async def create_subscription_checkout(
        self, request: CreateSubscriptionCheckout
    ) -> HostedSubscriptionCheckout:
        metadata = {
            "user_id": str(request.user_id),
            "order_id": str(request.order_id),
            "product_code": request.product_code,
            "product_version": str(request.product_version),
            "market": request.market,
            "currency": request.currency,
            "amount_minor": str(request.amount_minor),
            "credits": str(request.credits),
            "price_reference": request.price_reference,
            "consent_version": request.consent_version,
        }
        try:
            result = await asyncio.wait_for(
                asyncio.to_thread(
                    self._client.checkout.sessions.create,
                    {
                        "mode": "subscription",
                        "line_items": [{"price": request.price_reference, "quantity": 1}],
                        "success_url": request.success_url,
                        "cancel_url": request.cancel_url,
                        "client_reference_id": str(request.user_id),
                        "metadata": metadata,
                        "subscription_data": {"metadata": metadata},
                    },
                    options={"idempotency_key": request.idempotency_key},
                ),
                self._timeout,
            )
        except (TimeoutError, self._stripe.APIConnectionError) as exc:
            raise UnknownProviderOutcome from exc
        except self._stripe.StripeError as exc:
            raise PermanentProviderError(type(exc).__name__) from exc
        if not result.id or not result.url or str(result.mode) != "subscription":
            raise PermanentProviderError("malformed_subscription_checkout")
        expires = datetime.fromtimestamp(result.expires_at, UTC) if result.expires_at else None
        return HostedSubscriptionCheckout(
            checkout_id=str(result.id),
            url=str(result.url),
            status=str(result.status),
            expires_at=expires,
            live_mode=bool(result.livemode),
        )

    async def fetch_subscription_event(
        self, event_type: str, object_id: str
    ) -> SubscriptionProviderFact:
        if event_type.startswith("checkout.session."):
            session = await self._retrieve_checkout_subscription(object_id)
            subscription = await self._expanded_subscription(_value(session, "subscription"))
            invoice = _value(subscription, "latest_invoice")
            if invoice:
                return self._invoice_fact(invoice, subscription, event_type)
            return self._state_fact(subscription)
        if event_type.startswith("invoice."):
            invoice = await self._retrieve_invoice(object_id)
            subscription = await self._expanded_subscription(_value(invoice, "subscription"))
            return self._invoice_fact(invoice, subscription, event_type)
        subscription = await self._retrieve_subscription(object_id)
        if event_type == "subscription_reconciliation":
            invoice = _value(subscription, "latest_invoice")
            if invoice:
                return self._invoice_fact(invoice, subscription, event_type)
        return self._state_fact(subscription)

    async def fetch_subscription(self, subscription_id: str) -> SubscriptionProviderFact:
        return await self.fetch_subscription_event("subscription_reconciliation", subscription_id)

    async def cancel_subscription(self, subscription_id: str) -> SubscriptionStateFact:
        subscription = await self._update_subscription(
            subscription_id, {"cancel_at_period_end": True}
        )
        return self._state_fact(subscription)

    async def resume_subscription(self, subscription_id: str) -> SubscriptionStateFact:
        subscription = await self._update_subscription(
            subscription_id, {"cancel_at_period_end": False}
        )
        return self._state_fact(subscription)

    async def _retrieve_checkout_subscription(self, checkout_id: str) -> object:
        try:
            return await asyncio.wait_for(
                asyncio.to_thread(
                    self._client.checkout.sessions.retrieve,
                    checkout_id,
                    params={"expand": ["subscription.latest_invoice.payment_intent"]},
                ),
                self._timeout,
            )
        except (TimeoutError, self._stripe.APIConnectionError) as exc:
            raise UnknownProviderOutcome from exc
        except self._stripe.StripeError as exc:
            raise PermanentProviderError(type(exc).__name__) from exc

    async def _retrieve_invoice(self, invoice_id: str) -> object:
        try:
            return await asyncio.wait_for(
                asyncio.to_thread(
                    self._client.invoices.retrieve,
                    invoice_id,
                    params={"expand": ["subscription", "payment_intent"]},
                ),
                self._timeout,
            )
        except (TimeoutError, self._stripe.APIConnectionError) as exc:
            raise UnknownProviderOutcome from exc
        except self._stripe.StripeError as exc:
            raise PermanentProviderError(type(exc).__name__) from exc

    async def _retrieve_subscription(self, subscription_id: str) -> object:
        try:
            return await asyncio.wait_for(
                asyncio.to_thread(
                    self._client.subscriptions.retrieve,
                    subscription_id,
                    params={"expand": ["latest_invoice.payment_intent"]},
                ),
                self._timeout,
            )
        except (TimeoutError, self._stripe.APIConnectionError) as exc:
            raise UnknownProviderOutcome from exc
        except self._stripe.StripeError as exc:
            raise PermanentProviderError(type(exc).__name__) from exc

    async def _expanded_subscription(self, value: object) -> object:
        if isinstance(value, str):
            return await self._retrieve_subscription(value)
        if value is None:
            raise PermanentProviderError("missing_subscription")
        return value

    async def _update_subscription(self, subscription_id: str, params: dict[str, object]) -> object:
        try:
            return await asyncio.wait_for(
                asyncio.to_thread(
                    self._client.subscriptions.update,
                    subscription_id,
                    params,
                ),
                self._timeout,
            )
        except (TimeoutError, self._stripe.APIConnectionError) as exc:
            raise UnknownProviderOutcome from exc
        except self._stripe.StripeError as exc:
            raise PermanentProviderError(type(exc).__name__) from exc

    def _invoice_fact(
        self, invoice: object, subscription: object, event_type: str
    ) -> SubscriptionProviderFact:
        metadata = _metadata(subscription)
        user_id = _uuid_metadata(metadata, "user_id")
        subscription_id = _required_text(_value(subscription, "id"), "subscription_id")
        invoice_id = _required_text(_value(invoice, "id"), "invoice_id")
        period_start = _required_datetime(
            _value(invoice, "period_start") or _value(subscription, "current_period_start"),
            "period_start",
        )
        period_end = _required_datetime(
            _value(invoice, "period_end") or _value(subscription, "current_period_end"),
            "period_end",
        )
        amount_minor = _int_metadata(metadata, "amount_minor")
        currency = _text_metadata(metadata, "currency").upper()
        actual_amount = int(_value(invoice, "amount_paid") or _value(invoice, "amount_due") or 0)
        actual_currency = str(_value(invoice, "currency") or "").upper()
        if actual_amount != amount_minor or actual_currency != currency:
            raise ProviderStateMismatch("subscription commercial mismatch")
        status = str(_value(invoice, "status") or "")
        paid = status == "paid" or event_type == "invoice.paid"
        if paid:
            transitions = _value(invoice, "status_transitions")
            paid_at = _required_datetime(
                _value(transitions, "paid_at") or _value(invoice, "created"), "paid_at"
            )
            payment = _value(invoice, "payment_intent")
            payment_id = _required_text(
                _value(payment, "id") if payment is not None else invoice_id,
                "payment_id",
            )
            customer = _value(subscription, "customer") or _value(invoice, "customer")
            return PaidSubscriptionFact(
                user_id=user_id,
                provider="stripe",
                provider_customer_id=_required_text(
                    _value(customer, "id") if not isinstance(customer, str) else customer,
                    "customer_id",
                ),
                provider_subscription_id=subscription_id,
                provider_invoice_id=invoice_id,
                provider_payment_id=payment_id,
                product_code=_text_metadata(metadata, "product_code"),
                product_version=_int_metadata(metadata, "product_version"),
                market=_text_metadata(metadata, "market"),
                currency=currency,
                amount_minor=amount_minor,
                credits=_int_metadata(metadata, "credits"),
                price_reference=_text_metadata(metadata, "price_reference"),
                period_start=period_start,
                period_end=period_end,
                paid_at=paid_at,
                consent_version=_text_metadata(metadata, "consent_version"),
                live_mode=bool(_value(invoice, "livemode")),
            )
        return PastDueSubscriptionFact(
            provider="stripe",
            provider_subscription_id=subscription_id,
            provider_invoice_id=invoice_id,
            product_code=_text_metadata(metadata, "product_code"),
            product_version=_int_metadata(metadata, "product_version"),
            currency=currency,
            amount_minor=amount_minor,
            credits=_int_metadata(metadata, "credits"),
            period_start=period_start,
            period_end=period_end,
        )

    def _state_fact(self, subscription: object) -> SubscriptionStateFact:
        metadata = _metadata(subscription)
        return SubscriptionStateFact(
            user_id=_uuid_metadata(metadata, "user_id"),
            provider="stripe",
            provider_subscription_id=_required_text(_value(subscription, "id"), "subscription_id"),
            status=str(_value(subscription, "status") or "unknown"),
            current_period_start=_optional_datetime(_value(subscription, "current_period_start")),
            current_period_end=_optional_datetime(_value(subscription, "current_period_end")),
            cancel_at_period_end=bool(_value(subscription, "cancel_at_period_end")),
            canceled_at=_optional_datetime(_value(subscription, "canceled_at")),
        )


def _value(value: object, name: str) -> Any:
    if isinstance(value, Mapping):
        return value.get(name)
    return getattr(value, name, None)


def _metadata(value: object) -> dict[str, object]:
    raw = _value(value, "metadata")
    if isinstance(raw, Mapping):
        return {str(key): item for key, item in raw.items()}
    raise ProviderStateMismatch("subscription metadata missing")


def _text_metadata(metadata: Mapping[str, object], name: str) -> str:
    return _required_text(metadata.get(name), name)


def _int_metadata(metadata: Mapping[str, object], name: str) -> int:
    try:
        value = int(_required_text(metadata.get(name), name))
    except ValueError as exc:
        raise ProviderStateMismatch(f"invalid {name}") from exc
    if value <= 0:
        raise ProviderStateMismatch(f"invalid {name}")
    return value


def _uuid_metadata(metadata: Mapping[str, object], name: str) -> UUID:
    try:
        return UUID(_required_text(metadata.get(name), name))
    except ValueError as exc:
        raise ProviderStateMismatch(f"invalid {name}") from exc


def _required_text(value: object, name: str) -> str:
    text = str(value or "").strip()
    if not text:
        raise ProviderStateMismatch(f"missing {name}")
    return text


def _required_datetime(value: object, name: str) -> datetime:
    result = _optional_datetime(value)
    if result is None:
        raise ProviderStateMismatch(f"missing {name}")
    return result


def _optional_datetime(value: object) -> datetime | None:
    if value is None:
        return None
    if isinstance(value, datetime):
        if value.tzinfo is None:
            return value.replace(tzinfo=UTC)
        return value.astimezone(UTC)
    if isinstance(value, (int, float)):
        return datetime.fromtimestamp(value, UTC)
    raise ProviderStateMismatch("invalid provider timestamp")
