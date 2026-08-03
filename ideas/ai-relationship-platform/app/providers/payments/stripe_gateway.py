"""Stripe hosted Checkout adapter. Vendor objects never leave this module."""

import asyncio
import importlib
from collections.abc import Mapping
from datetime import UTC, datetime

from app.providers.payments.base import (
    PaymentPayloadError,
    PaymentSignatureError,
    PermanentProviderError,
    UnknownProviderOutcome,
)
from app.providers.payments.gateway import AuthoritativePayment, CreateCheckout, HostedCheckout


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
