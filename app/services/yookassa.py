"""REST API ЮKassa v3: создание платежа и проверка статуса."""

from __future__ import annotations

import base64
import uuid
from typing import Any

import httpx


class YookassaError(RuntimeError):
    pass


def _basic_auth(shop_id: str, secret_key: str) -> str:
    raw = f"{shop_id}:{secret_key}".encode()
    return base64.b64encode(raw).decode()


async def create_redirect_payment(
    *,
    shop_id: str,
    secret_key: str,
    amount_value_rub: str,
    return_url: str,
    description: str,
    metadata: dict[str, str],
) -> dict[str, Any]:
    headers = {
        "Authorization": f"Basic {_basic_auth(shop_id, secret_key)}",
        "Content-Type": "application/json",
        "Idempotence-Key": str(uuid.uuid4()),
    }
    body: dict[str, Any] = {
        "amount": {"value": amount_value_rub, "currency": "RUB"},
        "capture": True,
        "confirmation": {"type": "redirect", "return_url": return_url},
        "description": (description or "")[:128],
        "metadata": metadata,
    }
    async with httpx.AsyncClient(timeout=40) as client:
        r = await client.post("https://api.yookassa.ru/v3/payments", json=body, headers=headers)
        if r.status_code >= 400:
            raise YookassaError(f"HTTP {r.status_code}: {r.text}")
        return r.json()


async def get_payment(*, shop_id: str, secret_key: str, payment_id: str) -> dict[str, Any]:
    headers = {
        "Authorization": f"Basic {_basic_auth(shop_id, secret_key)}",
    }
    async with httpx.AsyncClient(timeout=30) as client:
        r = await client.get(f"https://api.yookassa.ru/v3/payments/{payment_id}", headers=headers)
        if r.status_code >= 400:
            raise YookassaError(f"HTTP {r.status_code}: {r.text}")
        return r.json()
