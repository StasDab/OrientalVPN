import asyncio
import json
import logging

from aiogram import Bot
from sqlalchemy import select

from app.config import settings
from app.db.models import Payment, User
from app.db.repositories import (
    create_or_extend_subscription,
    list_all_user_tg_ids,
    list_expired_active_subscriptions,
    list_pending_provision_events,
    list_subscriptions_needing_reminder,
    mark_reminder_sent,
    touch_event,
    update_payment_status,
)
from app.db.session import SessionLocal
from app.plans import LOCATION_TITLES, plan_days
from app.services.node_registry import pick_node_for_location
from app.services.retry import with_retry
from app.services.vpn_provider import MarzbanAdapter
from app.telegram_format import code_inline

log = logging.getLogger(__name__)


async def _disable_expired() -> None:
    async with SessionLocal() as session:
        expired = await list_expired_active_subscriptions(session)
        for sub in expired:
            try:
                provider = MarzbanAdapter(
                    panel_url=sub.node_api_url,
                    username=settings.panel_username,
                    password=settings.panel_password,
                )
                await provider.disable_access(sub.external_user_id)
                sub.status = "expired"
            except Exception:
                continue
        await session.commit()


async def _send_reminders(bot: Bot) -> None:
    async with SessionLocal() as session:
        due = await list_subscriptions_needing_reminder(
            session, hours_before=settings.reminder_hours_before
        )
        for sub in due:
            urow = await session.execute(select(User).where(User.id == sub.user_id))
            user = urow.scalar_one_or_none()
            if not user:
                continue
            loc = LOCATION_TITLES.get(sub.location_code, sub.location_code.upper())
            text = (
                f"Напоминание: ваша подписка VPN ({loc}) скоро закончится "
                f"(окончание ~ {sub.ends_at.strftime('%Y-%m-%d %H:%M')} UTC).\n"
                "Продлите доступ через /buy."
            )
            try:
                await bot.send_message(user.tg_id, text)
                await mark_reminder_sent(session, sub.id)
            except Exception:
                log.warning("reminder_send_failed", extra={"tg_id": user.tg_id})
        await session.commit()


async def _process_provision_events(bot: Bot) -> None:
    async with SessionLocal() as session:
        events = await list_pending_provision_events(session, limit=25)
        for ev in events:
            try:
                data = json.loads(ev.payload)
                payment_id = int(data["payment_id"])
                tg_user_id = int(data["tg_user_id"])
                plan_code = str(data["plan_code"])
                location_code = str(data["location_code"])
                plan_days_val = int(data.get("plan_days") or plan_days(plan_code))
            except (KeyError, ValueError, TypeError, json.JSONDecodeError):
                await touch_event(session, ev.id, status="failed", retries=ev.retries)
                continue

            pay_row = await session.get(Payment, payment_id)
            if not pay_row:
                await touch_event(session, ev.id, status="failed", retries=ev.retries)
                continue
            if pay_row.status == "paid":
                await touch_event(session, ev.id, status="done", retries=ev.retries)
                continue

            node = pick_node_for_location(location_code)
            if not node:
                nret = ev.retries + 1
                if nret >= settings.event_max_retries:
                    await touch_event(session, ev.id, status="failed", retries=nret)
                else:
                    await touch_event(session, ev.id, status="pending", retries=nret)
                continue

            provider = MarzbanAdapter(
                panel_url=node.api_url,
                username=settings.panel_username,
                password=settings.panel_password,
            )
            try:
                result = await with_retry(
                    lambda: provider.provision_access(
                        tg_user_id,
                        location_code,
                        node=node,
                        days=plan_days_val,
                    ),
                    retries=settings.provision_retries,
                    base_delay_seconds=1.0,
                )
            except Exception:
                nret = ev.retries + 1
                if nret >= settings.event_max_retries:
                    await touch_event(session, ev.id, status="failed", retries=nret)
                else:
                    await touch_event(session, ev.id, status="pending", retries=nret)
                continue

            urow = await session.execute(select(User).where(User.id == pay_row.user_id))
            db_user = urow.scalar_one_or_none()
            if not db_user:
                await touch_event(session, ev.id, status="failed", retries=ev.retries)
                continue

            await create_or_extend_subscription(
                session=session,
                user_id=db_user.id,
                external_user_id=result.external_user_id,
                subscription_url=result.subscription_url,
                location_code=location_code,
                node_api_url=node.api_url,
                duration_days=plan_days_val,
            )
            await update_payment_status(session, payment_id, "paid")
            await touch_event(session, ev.id, status="done", retries=ev.retries)
            try:
                await bot.send_message(
                    tg_user_id,
                    "Доступ выдан после ожидания.\n"
                    "Ссылка подписки (нажмите, чтобы скопировать):\n"
                    f"{code_inline(result.subscription_url)}\n"
                    "Инструкция: клиент → вставить ссылку → обновить профиль.",
                    parse_mode="HTML",
                    disable_web_page_preview=True,
                )
            except Exception:
                log.warning("provision_notify_failed", extra={"tg_id": tg_user_id})

        await session.commit()


async def background_jobs(bot: Bot) -> None:
    while True:
        try:
            await _disable_expired()
        except Exception:
            log.exception("disable_expired_failed")
        try:
            await _send_reminders(bot)
        except Exception:
            log.exception("reminders_failed")
        try:
            await _process_provision_events(bot)
        except Exception:
            log.exception("provision_events_failed")
        await asyncio.sleep(settings.check_interval_minutes * 60)
