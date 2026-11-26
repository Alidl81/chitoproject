"""Customer access and server-to-server verification for website orders."""

import hashlib
import hmac
import logging
import re
from datetime import timedelta
from urllib.parse import urlsplit

import httpx

from app.config import settings
from app.models import PaymentStage, now_utc

logger = logging.getLogger(__name__)
_GATEWAY = "https://payment.zarinpal.com"
_AUTHORITY = re.compile(r"A[A-Za-z0-9]{35}")
_STAGE_ID = re.compile(r"[a-f0-9-]{36}")
ACCESS_LIFETIME = timedelta(days=14)
MAX_AMOUNT_TOMAN = 100_000_000


class GatewayError(Exception):
    """The provider did not return a usable result."""


def checkout_url() -> str:
    return (settings.checkout_base_url or settings.public_base_url).rstrip("/")


def merchant() -> str:
    value = settings.zarinpal_merchant_id
    raw = value.get_secret_value().strip() if value else ""
    return raw if re.fullmatch(r"[A-Za-z0-9-]{36}", raw) else ""


def website_enabled() -> bool:
    return urlsplit(checkout_url()).scheme == "https"


def _signature(payload: str) -> str:
    key = settings.secret_key.get_secret_value().encode()
    return hmac.new(key, f"checkout-access:{payload}".encode(), hashlib.sha256).hexdigest()


def order_link(stage: PaymentStage) -> str:
    expires = int((now_utc() + ACCESS_LIFETIME).timestamp())
    payload = f"{stage.id}.{stage.version}.{expires}"
    return f"{checkout_url()}/orders#{payload}.{_signature(payload)}"


def access_scope(token: str) -> tuple[str, int] | None:
    parts = token.split(".", 3)
    if len(parts) != 4:
        return None
    stage_id, version, expires, signature = parts
    if (not _STAGE_ID.fullmatch(stage_id) or not re.fullmatch(r"[1-9][0-9]{0,5}", version)
            or not re.fullmatch(r"[0-9]{10}", expires) or not re.fullmatch(r"[a-f0-9]{64}", signature)
            or int(expires) <= now_utc().timestamp()):
        return None
    payload = ".".join(parts[:3])
    return (stage_id, int(version)) if hmac.compare_digest(signature, _signature(payload)) else None


def valid_authority(authority: object) -> bool:
    return isinstance(authority, str) and _AUTHORITY.fullmatch(authority) is not None


def pay_url(authority: str) -> str:
    return f"{_GATEWAY}/pg/StartPay/{authority}"


async def _provider(action: str, payload: dict) -> dict:
    merchant_id = merchant()
    if not merchant_id:
        raise GatewayError("merchant unavailable")
    try:
        async with httpx.AsyncClient(timeout=httpx.Timeout(10.0)) as client:
            response = await client.post(
                f"{_GATEWAY}/pg/v4/payment/{action}.json",
                json={"merchant_id": merchant_id, **payload},
            )
            body = response.json()
    except (httpx.HTTPError, ValueError) as exc:
        raise GatewayError("invalid provider response") from exc
    data = body.get("data") if isinstance(body, dict) else None
    if response.is_success and isinstance(data, dict) and data.get("code") in {100, 101}:
        return data
    error = body.get("errors") if isinstance(body, dict) else None
    code = error.get("code") if isinstance(error, dict) else None
    logger.warning("ZarinPal operation=%s status=%s provider_code=%s",
                   action, response.status_code, code if type(code) is int else None)
    raise GatewayError("provider did not confirm the operation")


async def open_payment(stage: PaymentStage, public_id: str) -> str:
    data = await _provider("request", {
        "amount": stage.amount_toman * 10,
        "currency": "IRR",
        "description": f"ChitoProject - {public_id} - {stage.number}",
        "callback_url": f"{checkout_url()}/orders/return",
        "metadata": {"order_id": f"{public_id}-{stage.number}"},
    })
    authority = data.get("authority")
    if data.get("code") != 100 or not valid_authority(authority):
        raise GatewayError("payment creation refused")
    return authority


async def verify_payment(stage: PaymentStage) -> str:
    data = await _provider("verify", {
        "amount": stage.amount_toman * 10,
        "authority": stage.gateway_authority,
    })
    reference = str(data.get("ref_id", ""))
    if data.get("code") not in {100, 101} or not re.fullmatch(r"[1-9][0-9]{0,35}", reference):
        raise GatewayError("payment not verified")
    return reference



async def payment_was_charged(stage: PaymentStage) -> bool:
    data = await _provider("inquiry", {"authority": stage.gateway_authority})
    return data.get("status") in {"PAID", "VERIFIED"}
