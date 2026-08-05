from datetime import UTC, datetime
from types import SimpleNamespace
from typing import Any, cast
from uuid import UUID, uuid4

import pytest

from app.providers.payments.base import ProviderStateMismatch
from app.providers.payments.stripe_gateway import StripeGateway
from app.providers.payments.subscription_gateway import (
    CreateSubscriptionCheckout,
    PaidSubscriptionFact,
    PastDueSubscriptionFact,
    SubscriptionStateFact,
)


class StripeError(Exception):
    pass


class APIConnectionError(StripeError):
    pass


class SignatureVerificationError(StripeError):
    pass


class FakeSessions:
    def __init__(self) -> None:
        self.created: tuple[dict[str, object], dict[str, str]] | None = None
        self.subscription: object | None = None

    def create(self, params: dict[str, object], *, options: dict[str, str]) -> object:
        self.created = params, options
        return SimpleNamespace(
            id="cs_sub_1",
            url="https://checkout.stripe.test/cs_sub_1",
            status="open",
            mode="subscription",
            expires_at=1_800_000_000,
            livemode=True,
        )

    def retrieve(self, _checkout_id: str, *, params: dict[str, object]) -> object:
        assert params["expand"]
        assert self.subscription is not None
        return {"subscription": self.subscription}


class FakeInvoices:
    def __init__(self, value: object) -> None:
        self.value = value

    def retrieve(self, _invoice_id: str, *, params: dict[str, object]) -> object:
        assert params["expand"]
        return self.value


class FakeSubscriptions:
    def __init__(self, value: object) -> None:
        self.value = value
        self.updated: tuple[str, dict[str, object]] | None = None

    def retrieve(self, _subscription_id: str, *, params: dict[str, object]) -> object:
        assert params["expand"]
        return self.value

    def update(self, subscription_id: str, params: dict[str, object]) -> object:
        self.updated = subscription_id, params
        result = dict(self.value) if isinstance(self.value, dict) else self.value
        if isinstance(result, dict):
            result["cancel_at_period_end"] = bool(params["cancel_at_period_end"])
        return result


class FakeClient:
    def __init__(self, invoice: object, subscription: object) -> None:
        self.checkout = SimpleNamespace(sessions=FakeSessions())
        self.invoices = FakeInvoices(invoice)
        self.subscriptions = FakeSubscriptions(subscription)


class FakeStripe:
    APIConnectionError = APIConnectionError
    StripeError = StripeError
    SignatureVerificationError = SignatureVerificationError


@pytest.fixture
def commercial() -> dict[str, str]:
    return {
        "user_id": str(uuid4()),
        "order_id": str(uuid4()),
        "product_code": "subscription_monthly",
        "product_version": "1",
        "market": "INTERNATIONAL",
        "currency": "EUR",
        "amount_minor": "990",
        "credits": "30",
        "price_reference": "price_monthly_eur",
        "consent_version": "billing-v1",
    }


def subscription_value(commercial: dict[str, str]) -> dict[str, object]:
    return {
        "id": "sub_1",
        "customer": "cus_1",
        "status": "active",
        "metadata": commercial,
        "current_period_start": 1_780_000_000,
        "current_period_end": 1_782_592_000,
        "cancel_at_period_end": False,
        "canceled_at": None,
    }


def invoice_value(
    subscription: dict[str, object], *, status: str = "paid", amount: int = 990
) -> dict[str, object]:
    return {
        "id": "in_1",
        "status": status,
        "amount_paid": amount if status == "paid" else 0,
        "amount_due": amount,
        "currency": "eur",
        "period_start": 1_780_000_000,
        "period_end": 1_782_592_000,
        "created": 1_780_000_100,
        "status_transitions": {"paid_at": 1_780_000_200 if status == "paid" else None},
        "payment_intent": {"id": "pi_1"},
        "livemode": True,
        "customer": "cus_1",
        "subscription": subscription,
    }


def gateway(invoice: object, subscription: object) -> tuple[StripeGateway, FakeClient]:
    value = object.__new__(StripeGateway)
    client = FakeClient(invoice, subscription)
    dynamic = cast(Any, value)
    dynamic._stripe = FakeStripe()
    dynamic._client = client
    dynamic._webhook_secret = "whsec_test"
    dynamic._timeout = 1
    return value, client


@pytest.mark.asyncio
async def test_subscription_checkout_uses_server_metadata_and_idempotency() -> None:
    empty_subscription: dict[str, object] = {}
    value, client = gateway({}, empty_subscription)
    request = CreateSubscriptionCheckout(
        user_id=uuid4(),
        order_id=uuid4(),
        product_code="subscription_monthly",
        product_version=1,
        amount_minor=990,
        currency="EUR",
        credits=30,
        price_reference="price_monthly_eur",
        market="INTERNATIONAL",
        consent_version="billing-v1",
        idempotency_key="subscription:checkout:1",
        success_url="https://pay.example/return",
        cancel_url="https://pay.example/return",
    )

    result = await value.create_subscription_checkout(request)

    assert result.checkout_id == "cs_sub_1"
    assert client.checkout.sessions.created is not None
    params, options = client.checkout.sessions.created
    metadata = cast(dict[str, str], params["metadata"])
    subscription_data = cast(dict[str, object], params["subscription_data"])
    assert params["mode"] == "subscription"
    assert params["line_items"] == [{"price": "price_monthly_eur", "quantity": 1}]
    assert metadata == subscription_data["metadata"]
    assert metadata["amount_minor"] == "990"
    assert options == {"idempotency_key": "subscription:checkout:1"}


@pytest.mark.asyncio
async def test_paid_invoice_normalizes_authoritative_period(
    commercial: dict[str, str],
) -> None:
    subscription = subscription_value(commercial)
    invoice = invoice_value(subscription)
    value, _ = gateway(invoice, subscription)

    fact = await value.fetch_subscription_event("invoice.paid", "in_1")

    assert isinstance(fact, PaidSubscriptionFact)
    assert fact.user_id == UUID(commercial["user_id"])
    assert fact.initial_order_id == UUID(commercial["order_id"])
    assert fact.provider_subscription_id == "sub_1"
    assert fact.provider_invoice_id == "in_1"
    assert fact.provider_payment_id == "pi_1"
    assert fact.amount_minor == 990
    assert fact.currency == "EUR"
    assert fact.credits == 30
    assert fact.period_start.tzinfo is UTC
    assert fact.live_mode is True


@pytest.mark.asyncio
async def test_failed_invoice_produces_past_due_without_credit_fact(
    commercial: dict[str, str],
) -> None:
    subscription = subscription_value(commercial)
    invoice = invoice_value(subscription, status="open")
    value, _ = gateway(invoice, subscription)

    fact = await value.fetch_subscription_event("invoice.payment_failed", "in_1")

    assert isinstance(fact, PastDueSubscriptionFact)
    assert fact.provider_invoice_id == "in_1"
    assert fact.amount_minor == 990


@pytest.mark.asyncio
async def test_invoice_amount_mismatch_fails_closed(commercial: dict[str, str]) -> None:
    subscription = subscription_value(commercial)
    value, _ = gateway(invoice_value(subscription, amount=991), subscription)

    with pytest.raises(ProviderStateMismatch):
        await value.fetch_subscription_event("invoice.paid", "in_1")


@pytest.mark.asyncio
async def test_cancel_and_resume_only_toggle_period_end_flag(
    commercial: dict[str, str],
) -> None:
    subscription = subscription_value(commercial)
    value, client = gateway(invoice_value(subscription), subscription)

    canceled = await value.cancel_subscription("sub_1")
    assert isinstance(canceled, SubscriptionStateFact)
    assert canceled.cancel_at_period_end is True
    assert client.subscriptions.updated == ("sub_1", {"cancel_at_period_end": True})

    resumed = await value.resume_subscription("sub_1")
    assert resumed.cancel_at_period_end is False
    assert client.subscriptions.updated == ("sub_1", {"cancel_at_period_end": False})


def test_optional_datetime_accepts_real_datetime(commercial: dict[str, str]) -> None:
    subscription = subscription_value(commercial)
    subscription["current_period_end"] = datetime(2026, 9, 1, tzinfo=UTC)
    value, _ = gateway(invoice_value(subscription), subscription)
    fact = value._state_fact(subscription)
    assert fact.current_period_end == datetime(2026, 9, 1, tzinfo=UTC)
