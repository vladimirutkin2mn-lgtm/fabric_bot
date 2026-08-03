"""Local mock checkout and provider-neutral webhook endpoints."""
# ruff: noqa: E501

import json
import time
from uuid import UUID, uuid4

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import HTMLResponse

from app.domain.products import ProductCatalog, format_minor
from app.providers.payments.base import (
    PaymentExpiredEventError,
    PaymentPayloadError,
    PaymentSignatureError,
)
from app.providers.payments.mock import MockPaymentProvider
from app.services.payment_service import PaymentCompletionOutcome, PaymentService

router = APIRouter(prefix="/payments", tags=["payments"])


@router.get("/return/{token}", response_class=HTMLResponse)
async def payment_return(token: UUID, request: Request) -> HTMLResponse:
    """Show internal state only; browser parameters can never complete an order."""
    order = await request.app.state.checkout_service.order_by_token(token)
    if order is None:
        raise HTTPException(404)
    label = {
        "completed": "Оплата получена",
        "failed": "Оплата не прошла",
        "cancelled": "Оплата отменена",
        "manual_review": "Оплата проверяется",
    }.get(order.status, "Оплата обрабатывается")
    return HTMLResponse(
        f"<!doctype html><title>Статус оплаты</title><h1>{label}</h1>"
        "<p>Вернитесь в бот. HeartSignal не собирает данные карты.</p>"
    )


def _services(request: Request) -> tuple[PaymentService, MockPaymentProvider, ProductCatalog]:
    return (
        request.app.state.payment_service,
        request.app.state.payment_provider,
        request.app.state.product_catalog,
    )


@router.get("/mock/checkout/{token}", response_class=HTMLResponse)
async def mock_checkout(token: UUID, request: Request) -> HTMLResponse:
    if request.app.state.settings.payment_provider != "mock":
        raise HTTPException(404)
    service, _, catalog = _services(request)
    order = await service.order_by_token(token)
    if order is None:
        raise HTTPException(404)
    product = catalog.get(order.product_code)
    if product is None:
        raise HTTPException(404)
    return HTMLResponse(
        f"<!doctype html><title>Тестовая оплата</title><h1>{product.title}</h1><p>{format_minor(order.amount_minor, order.currency)} · {order.credits} кредитов</p><p>Тестовая оплата — реальные деньги не списываются. Данные карты не собираются.</p><form method='post' action='/payments/mock/checkout/{token}/complete'><button>Завершить тестовую оплату</button></form>"
    )


@router.post("/mock/checkout/{token}/complete", response_class=HTMLResponse)
async def complete_mock_checkout(token: UUID, request: Request) -> HTMLResponse:
    if request.app.state.settings.payment_provider != "mock":
        raise HTTPException(404)
    service, provider, _ = _services(request)
    order = await service.order_by_token(token)
    if order is None or order.provider_checkout_id is None:
        raise HTTPException(404)
    payload = json.dumps(
        {
            "event_id": f"event-{uuid4()}",
            "checkout_id": order.provider_checkout_id,
            "payment_id": order.provider_payment_id or f"payment-{order.id}",
            "status": "paid",
            "amount_minor": order.amount_minor,
            "currency": order.currency,
        },
        separators=(",", ":"),
    ).encode()
    timestamp = int(time.time())
    event = await provider.verify_webhook(
        payload,
        {"X-Mock-Timestamp": str(timestamp), "X-Mock-Signature": provider.sign(payload, timestamp)},
    )
    outcome = await service.complete(event)
    if outcome not in {
        PaymentCompletionOutcome.COMPLETED,
        PaymentCompletionOutcome.ALREADY_COMPLETED,
    }:
        raise HTTPException(409)
    return HTMLResponse(
        "<!doctype html><title>Оплата завершена</title><h1>Тестовая оплата завершена</h1><p>Вернитесь в бот и обновите баланс.</p>"
    )


@router.post("/webhooks/{provider_name}")
async def payment_webhook(provider_name: str, request: Request) -> dict[str, str]:
    if provider_name != "mock" or request.app.state.settings.payment_provider != "mock":
        raise HTTPException(404)
    service, provider, _ = _services(request)
    payload = await request.body()
    try:
        event = await provider.verify_webhook(payload, request.headers)
    except PaymentSignatureError:
        raise HTTPException(401, "invalid signature") from None
    except PaymentExpiredEventError:
        raise HTTPException(400, "expired event") from None
    except PaymentPayloadError:
        raise HTTPException(400, "malformed event") from None
    outcome = await service.complete(event)
    if outcome is PaymentCompletionOutcome.ORDER_NOT_FOUND:
        raise HTTPException(404, "order not found")
    if outcome not in {
        PaymentCompletionOutcome.COMPLETED,
        PaymentCompletionOutcome.ALREADY_COMPLETED,
    }:
        raise HTTPException(409, "payment mismatch")
    return {"status": outcome.value}
