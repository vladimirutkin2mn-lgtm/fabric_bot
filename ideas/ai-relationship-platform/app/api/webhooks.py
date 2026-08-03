"""Authenticated, size-bounded production payment webhook endpoints."""

import hashlib
import ipaddress
import json
import logging
from collections.abc import Mapping

from fastapi import APIRouter, HTTPException, Request

from app.providers.payments.base import (
    PaymentPayloadError,
    PaymentProviderName,
    PaymentSignatureError,
)

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/webhooks", tags=["payment-webhooks"])


def resolve_source_ip(peer: str, headers: Mapping[str, str], trusted: str) -> str:
    """Resolve forwarding only from a trusted direct peer; reject ambiguous syntax."""
    try:
        direct = ipaddress.ip_address(peer)
        trusted_nets = [ipaddress.ip_network(x.strip()) for x in trusted.split(",") if x.strip()]
    except ValueError as exc:
        raise PaymentPayloadError("invalid source address") from exc
    forwarded = headers.get("forwarded")
    xff = headers.get("x-forwarded-for")
    if not forwarded and not xff:
        return str(direct)
    if not any(direct in network for network in trusted_nets):
        raise PaymentSignatureError("untrusted forwarding peer")
    if forwarded and xff:
        raise PaymentPayloadError("ambiguous forwarding headers")
    raw = xff or forwarded or ""
    if forwarded:
        parts = []
        for element in raw.split(","):
            fields = dict(item.strip().split("=", 1) for item in element.split(";") if "=" in item)
            if "for" not in fields:
                raise PaymentPayloadError("malformed Forwarded header")
            parts.append(fields["for"].strip('"[]'))
    else:
        parts = [part.strip() for part in raw.split(",")]
    try:
        chain = [ipaddress.ip_address(part) for part in parts]
    except ValueError as exc:
        raise PaymentPayloadError("malformed forwarding chain") from exc
    # Walk right-to-left over trusted hops; the first non-trusted address is the client.
    candidate = direct
    for address in reversed(chain):
        if not any(candidate in network for network in trusted_nets):
            break
        candidate = address
    return str(candidate)


def source_is_allowed(source: str, allowlist: str) -> bool:
    try:
        address = ipaddress.ip_address(source)
        return any(
            address in ipaddress.ip_network(value.strip())
            for value in allowlist.split(",")
            if value.strip()
        )
    except ValueError:
        return False


async def _body(request: Request) -> bytes:
    limit = request.app.state.settings.payment_webhook_max_bytes
    content_length = request.headers.get("content-length")
    if content_length and (not content_length.isdigit() or int(content_length) > limit):
        raise HTTPException(413, "payload too large")
    value = await request.body()
    if len(value) > limit:
        raise HTTPException(413, "payload too large")
    return value


@router.post("/stripe")
async def stripe_webhook(request: Request) -> dict[str, str]:
    body = await _body(request)
    signature = request.headers.get("stripe-signature")
    if not signature:
        raise HTTPException(401, "invalid signature")
    gateway = request.app.state.payment_gateways.get(PaymentProviderName.STRIPE)
    if gateway is None:
        raise HTTPException(503, "provider unavailable")
    try:
        event = gateway.verify_webhook(body, signature)
        data = event.get("data")
        obj = data.get("object") if isinstance(data, dict) else None
        event_id, event_type = str(event["id"]), str(event["type"])
        object_id = str(obj["id"]) if isinstance(obj, dict) else ""
        if not event_id or not object_id:
            raise PaymentPayloadError
    except PaymentSignatureError:
        raise HTTPException(401, "invalid signature") from None
    except (PaymentPayloadError, KeyError, TypeError):
        raise HTTPException(400, "malformed event") from None
    await request.app.state.webhook_inbox.accept(
        "stripe", event_id, event_type, object_id, hashlib.sha256(body).hexdigest()
    )
    return {"status": "accepted"}


@router.post("/yookassa")
async def yookassa_webhook(request: Request) -> dict[str, str]:
    body = await _body(request)
    settings = request.app.state.settings
    peer = request.client.host if request.client else ""
    try:
        source = resolve_source_ip(peer, request.headers, settings.yookassa_trusted_proxy_allowlist)
    except (PaymentPayloadError, PaymentSignatureError):
        raise HTTPException(403, "invalid source") from None
    if not source_is_allowed(source, settings.yookassa_webhook_ip_allowlist):
        raise HTTPException(403, "invalid source")
    try:
        value = json.loads(body)
        event_type = str(value["event"])
        obj = value["object"]
        object_id = str(obj["id"])
        if not event_type.startswith("payment.") or not object_id:
            raise ValueError
    except (ValueError, TypeError, KeyError, json.JSONDecodeError):
        raise HTTPException(400, "malformed event") from None
    event_id = hashlib.sha256(f"yookassa:{event_type}:{object_id}".encode()).hexdigest()
    await request.app.state.webhook_inbox.accept(
        "yookassa", event_id, event_type, object_id, hashlib.sha256(body).hexdigest()
    )
    return {"status": "accepted"}
