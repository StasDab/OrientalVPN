"""Тарифы и локации для инвойсов и валидации pre_checkout."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any


PLAN_MAP: dict[str, dict[str, Any]] = {
    "1m": {"title": "OrientalVPN · 30 дней", "amount": 19900, "days": 30},
    "3m": {"title": "OrientalVPN · 90 дней", "amount": 53000, "days": 90},
    "6m": {"title": "OrientalVPN · 180 дней", "amount": 97000, "days": 180},
    "12m": {"title": "OrientalVPN · 365 дней", "amount": 170000, "days": 365},
}

LOCATION_TITLES: dict[str, str] = {
    "all": "Все серверы",
    "de": "Germany",
    "nl": "Netherlands",
    "se": "EU — Stockholm",
    "eu_se": "EU — Stockholm",
    "fi": "Finland",
    "fi_hel": "Finland — Helsinki",
    "us": "USA — Charlotte (Sharlott)",
    "us_nc": "USA — Charlotte (Sharlott)",
}


@dataclass(frozen=True)
class InvoicePayload:
    plan_code: str
    location_code: str
    buyer_tg_id: int


def plan_days(plan_code: str) -> int:
    return int(PLAN_MAP.get(plan_code, {}).get("days", 30))


def decode_invoice_payload(raw: str) -> InvoicePayload | None:
    from app.services.node_registry import available_location_codes

    try:
        parts = raw.split(":")
        if len(parts) != 3:
            return None
        plan_code, location_code, uid_s = parts[0], parts[1].lower(), parts[2]
        if plan_code not in PLAN_MAP:
            return None
        locs = available_location_codes()
        if location_code == "all":
            if not locs:
                return None
            location_code = "all"
        elif location_code not in locs:
            return None
        buyer_tg_id = int(uid_s)
    except (ValueError, TypeError):
        return None
    return InvoicePayload(plan_code=plan_code, location_code=location_code, buyer_tg_id=buyer_tg_id)
