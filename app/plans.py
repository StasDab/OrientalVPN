"""Тарифы и локации для инвойсов и валидации pre_checkout."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any


PLAN_MAP: dict[str, dict[str, Any]] = {
    "1m": {"title": "VPN 1 month", "amount": 49000, "days": 30},
    "3m": {"title": "VPN 3 months", "amount": 120000, "days": 90},
    "12m": {"title": "VPN 12 months", "amount": 400000, "days": 365},
}

LOCATION_CODES: tuple[str, ...] = ("de", "nl", "se")

LOCATION_TITLES: dict[str, str] = {
    "de": "Germany",
    "nl": "Netherlands",
    "se": "Sweden",
}


@dataclass(frozen=True)
class InvoicePayload:
    plan_code: str
    location_code: str
    buyer_tg_id: int


def plan_days(plan_code: str) -> int:
    return int(PLAN_MAP.get(plan_code, {}).get("days", 30))


def decode_invoice_payload(raw: str) -> InvoicePayload | None:
    try:
        parts = raw.split(":")
        if len(parts) != 3:
            return None
        plan_code, location_code, uid_s = parts[0], parts[1].lower(), parts[2]
        if plan_code not in PLAN_MAP:
            return None
        if location_code not in LOCATION_CODES:
            return None
        buyer_tg_id = int(uid_s)
    except (ValueError, TypeError):
        return None
    return InvoicePayload(plan_code=plan_code, location_code=location_code, buyer_tg_id=buyer_tg_id)
