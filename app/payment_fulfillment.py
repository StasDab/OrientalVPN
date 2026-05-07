"""Общая выдача VPN после подтверждённой оплаты (Telegram Payments / ЮKassa)."""

from __future__ import annotations

import logging

from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.db.models import Payment
from app.db.repositories import (
    create_event,
    create_or_extend_subscription,
    update_payment_status,
)
from app.plans import decode_invoice_payload, plan_days
from app.services.node_registry import pick_node_for_location, pick_primary_node
from app.services.retry import with_retry
from app.services.vpn_provider import MarzbanAdapter

log = logging.getLogger(__name__)


class ProvisionOutcome:
    __slots__ = ("ok", "no_node", "marzban_error", "already_done", "subscription_url")

    def __init__(
        self,
        *,
        ok: bool = False,
        no_node: bool = False,
        marzban_error: bool = False,
        already_done: bool = False,
        subscription_url: str | None = None,
    ) -> None:
        self.ok = ok
        self.no_node = no_node
        self.marzban_error = marzban_error
        self.already_done = already_done
        self.subscription_url = subscription_url


async def fulfill_paid_payment_row(
    session: AsyncSession,
    pay_row: Payment,
    *,
    buyer_tg_id: int,
) -> ProvisionOutcome:
    """
    Идемпотентно: если pay_row уже paid — возвращает already_done.
    Иначе пытается выдать Marzban и ставит paid.
    """
    if pay_row.status == "paid":
        return ProvisionOutcome(already_done=True)

    if pay_row.status not in ("pending_provision", "pending_yookassa"):
        return ProvisionOutcome()

    inv = decode_invoice_payload(pay_row.invoice_payload or "")
    if not inv:
        log.error("fulfill_bad_invoice_payload", extra={"payment_id": pay_row.id})
        return ProvisionOutcome()

    if inv.buyer_tg_id != buyer_tg_id:
        log.warning(
            "fulfill_tg_mismatch",
            extra={"payment_id": pay_row.id, "expected": inv.buyer_tg_id, "got": buyer_tg_id},
        )
        return ProvisionOutcome()

    plan_code = inv.plan_code
    location_code = inv.location_code
    plan_days_val = plan_days(plan_code)

    selected_node = (
        pick_primary_node() if location_code == "all" else pick_node_for_location(location_code)
    )
    if not selected_node:
        await create_event(
            session,
            "provision_payment",
            {
                "payment_id": pay_row.id,
                "tg_user_id": buyer_tg_id,
                "plan_code": plan_code,
                "location_code": location_code,
                "plan_days": plan_days_val,
            },
        )
        await update_payment_status(session, pay_row.id, "pending_provision")
        return ProvisionOutcome(no_node=True)

    provider = MarzbanAdapter(
        panel_url=selected_node.api_url,
        username=settings.panel_username,
        password=settings.panel_password,
    )
    try:
        result = await with_retry(
            lambda: provider.provision_access(
                buyer_tg_id,
                location_code,
                node=selected_node,
                days=plan_days_val,
            ),
            retries=settings.provision_retries,
            base_delay_seconds=1.0,
        )
    except Exception:
        log.exception("fulfill_marzban_failed", extra={"payment_id": pay_row.id})
        await create_event(
            session,
            "provision_payment",
            {
                "payment_id": pay_row.id,
                "tg_user_id": buyer_tg_id,
                "plan_code": plan_code,
                "location_code": location_code,
                "plan_days": plan_days_val,
            },
        )
        await update_payment_status(session, pay_row.id, "pending_provision")
        return ProvisionOutcome(marzban_error=True)

    await create_or_extend_subscription(
        session=session,
        user_id=pay_row.user_id,
        external_user_id=result.external_user_id,
        subscription_url=result.subscription_url,
        location_code="all",
        node_api_url=selected_node.api_url,
        duration_days=plan_days_val,
        panel_ends_at=result.ends_at,
    )
    await update_payment_status(session, pay_row.id, "paid")
    return ProvisionOutcome(ok=True, subscription_url=result.subscription_url)
