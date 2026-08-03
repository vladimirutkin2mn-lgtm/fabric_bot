"""Crash-safe, server-authoritative one-time checkout orchestration."""

import hashlib
import hmac
import os
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import cast
from uuid import UUID, uuid4

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.config import Settings
from app.db.models import BillingJob, BillingOutboxEvent, PaymentOrder, User
from app.domain.billing import BillingCatalog, PurchaseMode
from app.providers.payments.base import (
    BillingMarket,
    PaymentProviderName,
    PermanentProviderError,
    UnknownProviderOutcome,
)
from app.providers.payments.gateway import CreateCheckout, HostedCheckout, OneTimePaymentGateway
from app.services.receipt_contact import InvalidReceiptContact, validate_receipt_contact


class CheckoutRejected(Exception):
    pass


@dataclass(frozen=True)
class OneTimeCheckoutResult:
    order_id: UUID
    checkout_token: UUID
    url: str | None
    status: str


class ReceiptContactCipher:
    """Authenticated temporary storage with a billing-specific derived key."""

    def __init__(self, secret: str) -> None:
        self._key = hashlib.sha256(b"heartsignal:billing-receipt:v1\0" + secret.encode()).digest()

    def encrypt(self, value: str) -> bytes:
        nonce = os.urandom(16)
        stream = hashlib.shake_256(self._key + nonce).digest(len(value.encode()))
        ciphertext = bytes(a ^ b for a, b in zip(value.encode(), stream, strict=True))
        return nonce + hmac.digest(self._key, nonce + ciphertext, "sha256") + ciphertext

    def decrypt(self, value: bytes) -> str:
        nonce, tag, ciphertext = value[:16], value[16:48], value[48:]
        if not hmac.compare_digest(tag, hmac.digest(self._key, nonce + ciphertext, "sha256")):
            raise CheckoutRejected("receipt contact storage is invalid")
        stream = hashlib.shake_256(self._key + nonce).digest(len(ciphertext))
        return bytes(a ^ b for a, b in zip(ciphertext, stream, strict=True)).decode()


class CheckoutService:
    def __init__(
        self,
        sessions: async_sessionmaker[AsyncSession],
        settings: Settings,
        catalog: BillingCatalog,
        gateways: dict[PaymentProviderName, OneTimePaymentGateway],
    ) -> None:
        self._sessions, self._settings, self._catalog, self._gateways = (
            sessions,
            settings,
            catalog,
            gateways,
        )
        self._cipher = ReceiptContactCipher(settings.content_encryption_key.get_secret_value())

    async def order_by_token(self, token: UUID) -> PaymentOrder | None:
        async with self._sessions() as session:
            return cast(
                PaymentOrder | None,
                await session.scalar(
                    select(PaymentOrder).where(PaymentOrder.checkout_token == token)
                ),
            )

    async def create_one_time_checkout(
        self,
        user_id: UUID,
        product_code: str,
        market: BillingMarket | str,
        currency: str,
        receipt_contact: str | None = None,
    ) -> OneTimeCheckoutResult:
        if not self._settings.permits_new_checkout():
            raise CheckoutRejected("billing unavailable")
        try:
            offer = self._catalog.resolve_product_offer(product_code, market, currency)
        except LookupError as exc:
            raise CheckoutRejected("unsupported offer") from exc
        if offer.purchase_mode is not PurchaseMode.ONE_TIME:
            raise CheckoutRejected("subscriptions unavailable")
        enabled = (
            self._settings.yookassa_enabled
            if offer.provider is PaymentProviderName.YOOKASSA
            else self._settings.stripe_enabled
        )
        if not enabled or offer.provider not in self._gateways:
            raise CheckoutRejected("provider unavailable")
        if (
            offer.provider is PaymentProviderName.YOOKASSA
            and self._settings.yookassa_receipts_required
        ):
            try:
                if not receipt_contact:
                    raise InvalidReceiptContact
                validate_receipt_contact(receipt_contact)
            except InvalidReceiptContact:
                raise CheckoutRejected("valid receipt contact required") from None
        attempt = uuid4()
        now = datetime.now(UTC)
        async with self._sessions.begin() as session:
            if (
                await session.scalar(select(User).where(User.id == user_id).with_for_update())
                is None
            ):
                raise CheckoutRejected("user not found")
            order = await session.scalar(
                select(PaymentOrder)
                .where(
                    PaymentOrder.user_id == user_id,
                    PaymentOrder.provider == offer.provider.value,
                    PaymentOrder.product_code == offer.product_code.value,
                    PaymentOrder.market == offer.market.value,
                    PaymentOrder.currency == offer.currency,
                    PaymentOrder.status.in_(("creating", "pending")),
                )
                .with_for_update()
            )
            if order and order.status == "pending" and order.checkout_url:
                return OneTimeCheckoutResult(
                    order.id, order.checkout_token, order.checkout_url, order.status
                )
            if (
                order
                and order.checkout_creation_started_at
                and (now - order.checkout_creation_started_at).total_seconds()
                < self._settings.checkout_creation_lease_seconds
            ):
                return OneTimeCheckoutResult(
                    order.id, order.checkout_token, order.checkout_url, order.status
                )
            if order is None:
                order = PaymentOrder(
                    user_id=user_id,
                    provider=offer.provider.value,
                    product_code=offer.product_code.value,
                    status="creating",
                    credits=offer.credits,
                    amount_minor=offer.amount_minor,
                    currency=offer.currency,
                    market=offer.market.value,
                    mode="one_time",
                    product_version=offer.product_version,
                    commercial_snapshot={
                        "product_code": offer.product_code.value,
                        "product_version": offer.product_version,
                        "credits": offer.credits,
                        "amount_minor": offer.amount_minor,
                        "currency": offer.currency,
                        "provider": offer.provider.value,
                        "market": offer.market.value,
                        "price_reference": offer.price_reference,
                        "billing_period": None,
                    },
                )
                session.add(order)
                await session.flush()
                order.idempotency_key = f"checkout:create:{order.id}:v1"
            order.checkout_creation_attempt_id, order.checkout_creation_started_at = attempt, now
            if receipt_contact:
                order.encrypted_receipt_contact = self._cipher.encrypt(receipt_contact)
            request = CreateCheckout(
                str(order.id),
                order.product_code,
                order.product_version,
                order.amount_minor,
                order.currency,
                offer.price_reference,
                order.idempotency_key or "",
                f"{self._settings.payment_public_base_url}/payments/return/{order.checkout_token}",
                f"{self._settings.payment_public_base_url}/payments/return/{order.checkout_token}",
                receipt_contact,
            )
            order_id, token = order.id, order.checkout_token
        try:
            checkout = await self._gateways[offer.provider].create_checkout(request)
        except UnknownProviderOutcome:
            await self._unknown(order_id, attempt, offer.provider.value)
            return OneTimeCheckoutResult(order_id, token, None, "creating")
        except PermanentProviderError:
            await self._failed(order_id, attempt)
            raise CheckoutRejected("provider rejected checkout") from None
        await self._save(order_id, attempt, checkout)
        return OneTimeCheckoutResult(order_id, token, checkout.url, "pending")

    async def _save(self, order_id: UUID, attempt: UUID, value: HostedCheckout) -> None:
        async with self._sessions.begin() as session:
            order = await session.get(PaymentOrder, order_id, with_for_update=True)
            if not order or order.checkout_creation_attempt_id != attempt:
                return
            order.provider_checkout_id, order.checkout_url, order.status = (
                value.checkout_id,
                value.url,
                "pending",
            )
            order.provider_payment_id, order.provider_status = value.payment_id, value.status
            order.provider_request_id, order.checkout_expires_at = (
                value.request_id,
                value.expires_at,
            )
            order.provider_live_mode = value.live_mode
            order.encrypted_receipt_contact = None
            session.add(
                BillingOutboxEvent(
                    aggregate_type="payment_order",
                    aggregate_id=str(order.id),
                    event_type="checkout_started",
                    payload={"product_code": order.product_code, "provider": order.provider},
                    idempotency_key=f"checkout_started:{order.id}",
                )
            )

    async def _unknown(self, order_id: UUID, attempt: UUID, provider: str) -> None:
        async with self._sessions.begin() as session:
            order = await session.get(PaymentOrder, order_id, with_for_update=True)
            if not order or order.checkout_creation_attempt_id != attempt:
                return
            order.provider_status = "unknown"
            key = f"reconcile:{order_id}"
            job = await session.scalar(
                select(BillingJob).where(BillingJob.idempotency_key == key).with_for_update()
            )
            if job is None:
                session.add(
                    BillingJob(
                        job_type="payment_reconciliation",
                        provider=provider,
                        object_type="payment_order",
                        object_id=str(order_id),
                        idempotency_key=key,
                    )
                )
            elif job.status in {"completed", "failed"}:
                job.status = "pending"
                job.available_at = datetime.now(UTC)

    async def _failed(self, order_id: UUID, attempt: UUID) -> None:
        async with self._sessions.begin() as session:
            order = await session.get(PaymentOrder, order_id, with_for_update=True)
            if not order or order.checkout_creation_attempt_id != attempt:
                return
            order.status = "failed"
            order.failure_code = "provider_rejected_checkout"
            order.encrypted_receipt_contact = None
            session.add(
                BillingOutboxEvent(
                    aggregate_type="payment_order",
                    aggregate_id=str(order.id),
                    event_type="payment_failed",
                    payload={"product_code": order.product_code, "provider": order.provider},
                    idempotency_key=f"payment_failed:{order.id}",
                )
            )
