"""Тарифы и payload инвойсов (тариф / пополнение баланса)."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from app.services.node_registry import available_location_codes

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
class PlanInvoicePayload:
    plan_code: str
    location_code: str
    buyer_tg_id: int
    balance_applied_minor: int = 0
    # Скидочный промо: id в БД; при списании/рефералах сумма считается по номиналу тарифа после скидки.
    promo_id: int | None = None


@dataclass(frozen=True)
class TopupInvoicePayload:
    buyer_tg_id: int
    amount_minor: int


def plan_days(plan_code: str) -> int:
    return int(PLAN_MAP.get(plan_code, {}).get("days", 30))


def plan_amount_minor(plan_code: str) -> int:
    return int(PLAN_MAP.get(plan_code, {}).get("amount", 0))


def decode_topup_invoice_payload(raw: str) -> TopupInvoicePayload | None:
    parts = (raw or "").strip().split(":")
    if len(parts) != 3 or parts[0] != "topup":
        return None
    try:
        tg_id = int(parts[1])
        amount = int(parts[2])
    except ValueError:
        return None
    if amount <= 0 or tg_id <= 0:
        return None
    return TopupInvoicePayload(buyer_tg_id=tg_id, amount_minor=amount)


def decode_plan_invoice_payload(raw: str) -> PlanInvoicePayload | None:
    parts = (raw or "").strip().split(":")
    if len(parts) < 3 or parts[0] == "topup":
        return None
    plan_code = parts[0]
    location_code = parts[1].lower()
    if plan_code not in PLAN_MAP:
        return None
    locs = available_location_codes()
    try:
        buyer_tg_id = int(parts[2])
    except ValueError:
        return None
    balance_applied_minor = 0
    if len(parts) >= 4:
        try:
            balance_applied_minor = max(0, int(parts[3]))
        except ValueError:
            return None
    promo_id: int | None = None
    if len(parts) >= 5:
        try:
            raw_pi = int(parts[4])
            promo_id = raw_pi if raw_pi > 0 else None
        except ValueError:
            return None
    if location_code == "all":
        if not locs:
            return None
    elif location_code not in locs:
        return None
    plan_amount = int(PLAN_MAP[plan_code]["amount"])
    if promo_id is None and balance_applied_minor > plan_amount:
        return None
    return PlanInvoicePayload(
        plan_code=plan_code,
        location_code=location_code,
        buyer_tg_id=buyer_tg_id,
        balance_applied_minor=balance_applied_minor,
        promo_id=promo_id,
    )


def encode_plan_invoice_payload(
    *,
    plan_code: str,
    location_code: str,
    buyer_tg_id: int,
    balance_applied_minor: int,
    promo_id: int | None = None,
) -> str:
    base = f"{plan_code}:{location_code}:{buyer_tg_id}:{balance_applied_minor}"
    if promo_id and promo_id > 0:
        return f"{base}:{promo_id}"
    return base


def decode_invoice_payload(raw: str) -> PlanInvoicePayload | None:
    """Совместимость: только тарифные инвойсы (не topup)."""
    return decode_plan_invoice_payload(raw)


def format_topup_payload(buyer_tg_id: int, amount_minor: int) -> str:
    return f"topup:{buyer_tg_id}:{amount_minor}"
