from datetime import UTC, datetime
from typing import cast
from uuid import uuid4

import pytest

from app.providers.payments.base import ProviderStateMismatch
from app.providers.payments.subscription_gateway import (
    CreateSubscriptionCheckout,
    InitialSubscriptionFailedFact,
    PaidSubscriptionFact,
    RenewSubscription,
    next_month_boundary,
)
from app.providers.payments.yookassa_gateway import YooKassaGateway
from app.services.sensitive_content import AESGCMSensitiveContentCipher, ContentPurpose


class FakeResponse:
    def __init__(self, value: dict[str, object]) -> None:
        self.status_code = 200
        self._value = value
        self.headers = {"X-Request-Id": "request-1"}

    def json(self) -> dict[str, object]:
        return self._value


class FakeHttpClient:
    def __init__(self) -> None:
        self.posts: list[tuple[dict[str, object], dict[str, str]]] = []
        self.payments: dict[str, dict[str, object]] = {}
        self.next_status = "succeeded"
        self.amount_delta = 0

    async def __aenter__(self) -> "FakeHttpClient":
        return self

    async def __aexit__(self, *_: object) -> None:
        return None

    async def post(
        self,
        _url: str,
        *,
        auth: tuple[str, str],
        headers: dict[str, str],
        json: dict[str, object],
    ) -> FakeResponse:
        assert auth == ("shop", "secret")
        self.posts.append((json, headers))
        payment_id = f"payment-{len(self.posts)}"
        amount = cast(dict[str, str], json["amount"])
        value = {
            "id": payment_id,
            "status": self.next_status,
            "paid": self.next_status == "succeeded",
            "test": True,
            "created_at": "2026-08-05T08:00:00Z",
            "captured_at": "2026-08-05T08:00:01Z",
            "amount": {
                "value": f"{float(amount['value']) + self.amount_delta / 100:.2f}",
                "currency": amount["currency"],
            },
            "metadata": json.get("metadata", {}),
            "confirmation": {"confirmation_url": f"https://pay.test/{payment_id}"},
            "payment_method": {"id": "pm-secret-value", "saved": True},
        }
        self.payments[payment_id] = value
        return FakeResponse(value)

    async def get(self, url: str, *, auth: tuple[str, str]) -> FakeResponse:
        assert auth == ("shop", "secret")
        return FakeResponse(self.payments[url.rsplit("/", 1)[-1]])


@pytest.fixture
def cipher() -> AESGCMSensitiveContentCipher:
    return AESGCMSensitiveContentCipher("test-payment-method-key")


@pytest.fixture
def transport(monkeypatch: pytest.MonkeyPatch) -> FakeHttpClient:
    fake = FakeHttpClient()
    monkeypatch.setattr(
        "app.providers.payments.yookassa_gateway.httpx.AsyncClient",
        lambda **_: fake,
    )
    return fake


def gateway(cipher: AESGCMSensitiveContentCipher) -> YooKassaGateway:
    return YooKassaGateway("shop", "secret", payment_method_cipher=cipher)


def checkout_request() -> CreateSubscriptionCheckout:
    return CreateSubscriptionCheckout(
        user_id=uuid4(),
        order_id=uuid4(),
        product_code="subscription_monthly",
        product_version=1,
        amount_minor=99_000,
        currency="RUB",
        credits=30,
        price_reference="catalog:subscription_monthly:rub:v1",
        market="RU",
        consent_version="billing-v1",
        idempotency_key="subscription:checkout:order:v1",
        success_url="https://pay.example/return",
        cancel_url="https://pay.example/return",
        receipt_contact="receipt@example.com",
    )


@pytest.mark.asyncio
async def test_initial_checkout_saves_method_and_returns_only_encrypted_envelope(
    cipher: AESGCMSensitiveContentCipher,
    transport: FakeHttpClient,
) -> None:
    value = gateway(cipher)
    request = checkout_request()

    hosted = await value.create_subscription_checkout(request)
    fact = await value.fetch_subscription_event("invoice.paid", hosted.checkout_id)

    payload, headers = transport.posts[0]
    assert payload["save_payment_method"] is True
    assert payload["capture"] is True
    assert headers["Idempotence-Key"] == request.idempotency_key
    assert isinstance(fact, PaidSubscriptionFact)
    assert fact.provider_subscription_id == f"yookassa:{request.order_id}"
    assert fact.initial_order_id == request.order_id
    assert fact.encrypted_payment_method is not None
    assert b"pm-secret-value" not in fact.encrypted_payment_method
    assert cipher.decrypt_json(ContentPurpose.PAYMENT_METHOD, fact.encrypted_payment_method) == {
        "id": "pm-secret-value"
    }
    assert fact.period_end == next_month_boundary(fact.period_start)


@pytest.mark.asyncio
async def test_renewal_decrypts_method_only_inside_adapter_and_reuses_idempotency(
    cipher: AESGCMSensitiveContentCipher,
    transport: FakeHttpClient,
) -> None:
    value = gateway(cipher)
    encrypted = cipher.encrypt_json(ContentPurpose.PAYMENT_METHOD, {"id": "pm-secret-value"})
    period_start = datetime(2026, 9, 5, 8, tzinfo=UTC)
    request = RenewSubscription(
        user_id=uuid4(),
        subscription_id=uuid4(),
        provider_subscription_id="yookassa:subscription",
        product_code="subscription_monthly",
        product_version=1,
        amount_minor=99_000,
        currency="RUB",
        credits=30,
        price_reference="catalog:subscription_monthly:rub:v1",
        market="RU",
        consent_version="billing-v1",
        period_start=period_start,
        period_end=next_month_boundary(period_start),
        idempotency_key="subscription:renewal:stable:payment:v1",
        encrypted_payment_method=encrypted,
    )

    fact = await value.renew_subscription(request)

    payload, headers = transport.posts[0]
    assert payload["payment_method_id"] == "pm-secret-value"
    assert payload["capture"] is True
    assert "confirmation" not in payload
    assert headers["Idempotence-Key"] == request.idempotency_key
    assert isinstance(fact, PaidSubscriptionFact)
    assert fact.initial_order_id is None
    assert fact.provider_subscription_id == request.provider_subscription_id
    assert fact.period_start == request.period_start
    assert fact.period_end == request.period_end
    assert fact.encrypted_payment_method is None


@pytest.mark.asyncio
async def test_canceled_initial_payment_closes_order_without_subscription_fact(
    cipher: AESGCMSensitiveContentCipher,
    transport: FakeHttpClient,
) -> None:
    transport.next_status = "canceled"
    value = gateway(cipher)
    request = checkout_request()

    hosted = await value.create_subscription_checkout(request)
    fact = await value.fetch_subscription_event("invoice.payment_failed", hosted.checkout_id)

    assert isinstance(fact, InitialSubscriptionFailedFact)
    assert fact.order_id == request.order_id
    assert fact.provider_status == "canceled"


@pytest.mark.asyncio
async def test_subscription_amount_mismatch_fails_closed(
    cipher: AESGCMSensitiveContentCipher,
    transport: FakeHttpClient,
) -> None:
    transport.amount_delta = 1
    value = gateway(cipher)
    hosted = await value.create_subscription_checkout(checkout_request())

    with pytest.raises(ProviderStateMismatch):
        await value.fetch_subscription_event("invoice.paid", hosted.checkout_id)


def test_next_month_clamps_end_of_month() -> None:
    assert next_month_boundary(datetime(2027, 1, 31, 12, tzinfo=UTC)) == datetime(
        2027, 2, 28, 12, tzinfo=UTC
    )
