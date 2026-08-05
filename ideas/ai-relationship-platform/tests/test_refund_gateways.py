from types import SimpleNamespace
from typing import Any, cast
from uuid import uuid4

import pytest

from app.providers.payments.refund_gateway import CreateRefund
from app.providers.payments.stripe_refund_gateway import StripeRefundGateway
from app.providers.payments.yookassa_refund_gateway import YooKassaRefundGateway


class StripeError(Exception):
    pass


class APIConnectionError(StripeError):
    pass


class FakeRefunds:
    def __init__(self) -> None:
        self.created: tuple[dict[str, object], dict[str, str]] | None = None
        self.value = SimpleNamespace(
            id="re_1",
            payment_intent="pi_1",
            status="succeeded",
            amount=500,
            currency="eur",
            livemode=True,
            failure_reason=None,
        )

    def create(self, params: dict[str, object], *, options: dict[str, str]) -> object:
        self.created = params, options
        return self.value

    def retrieve(self, refund_id: str) -> object:
        assert refund_id == "re_1"
        return self.value


class FakeStripe:
    APIConnectionError = APIConnectionError
    StripeError = StripeError


class FakeStripeClient:
    def __init__(self) -> None:
        self.refunds = FakeRefunds()


def stripe_gateway() -> tuple[StripeRefundGateway, FakeStripeClient]:
    value = object.__new__(StripeRefundGateway)
    client = FakeStripeClient()
    dynamic = cast(Any, value)
    dynamic._stripe = FakeStripe()
    dynamic._client = client
    dynamic._timeout = 1
    return value, client


def request(provider_payment_id: str = "pi_1", currency: str = "EUR") -> CreateRefund:
    return CreateRefund(
        user_id=uuid4(),
        refund_request_id=uuid4(),
        provider_payment_id=provider_payment_id,
        amount_minor=500,
        currency=currency,
        reason="requested_by_customer",
        idempotency_key="refund:stable-key",
    )


@pytest.mark.asyncio
async def test_stripe_refund_uses_payment_intent_amount_and_idempotency() -> None:
    gateway, client = stripe_gateway()

    fact = await gateway.create_refund(request())

    assert fact.provider == "stripe"
    assert fact.provider_refund_id == "re_1"
    assert fact.provider_payment_id == "pi_1"
    assert fact.status == "succeeded"
    assert fact.amount_minor == 500
    assert fact.currency == "EUR"
    assert client.refunds.created is not None
    params, options = client.refunds.created
    assert params["payment_intent"] == "pi_1"
    assert params["amount"] == 500
    assert options == {"idempotency_key": "refund:stable-key"}


class FakeResponse:
    def __init__(self, value: dict[str, object]) -> None:
        self.status_code = 200
        self._value = value

    def json(self) -> dict[str, object]:
        return self._value


class FakeHttpClient:
    def __init__(self) -> None:
        self.posts: list[tuple[dict[str, object], dict[str, str]]] = []
        self.value: dict[str, object] = {
            "id": "rf_1",
            "payment_id": "payment_1",
            "status": "succeeded",
            "amount": {"value": "5.00", "currency": "RUB"},
            "test": True,
        }

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
        return FakeResponse(self.value)

    async def get(self, _url: str, *, auth: tuple[str, str]) -> FakeResponse:
        assert auth == ("shop", "secret")
        return FakeResponse(self.value)


@pytest.fixture
def yookassa_transport(monkeypatch: pytest.MonkeyPatch) -> FakeHttpClient:
    fake = FakeHttpClient()
    monkeypatch.setattr(
        "app.providers.payments.yookassa_refund_gateway.httpx.AsyncClient",
        lambda **_: fake,
    )
    return fake


@pytest.mark.asyncio
async def test_yookassa_refund_uses_payment_amount_and_stable_idempotency(
    yookassa_transport: FakeHttpClient,
) -> None:
    gateway = YooKassaRefundGateway("shop", "secret")

    fact = await gateway.create_refund(request("payment_1", "RUB"))

    assert fact.provider == "yookassa"
    assert fact.provider_refund_id == "rf_1"
    assert fact.provider_payment_id == "payment_1"
    assert fact.status == "succeeded"
    assert fact.amount_minor == 500
    assert fact.live_mode is False
    payload, headers = yookassa_transport.posts[0]
    assert payload["payment_id"] == "payment_1"
    assert payload["amount"] == {"value": "5.00", "currency": "RUB"}
    assert headers["Idempotence-Key"] == "refund:stable-key"


def test_yookassa_partial_refund_capability_is_policy_controlled() -> None:
    assert YooKassaRefundGateway("shop", "secret").refund_capabilities.partial_refunds
    assert not YooKassaRefundGateway(
        "shop", "secret", partial_refunds=False
    ).refund_capabilities.partial_refunds
