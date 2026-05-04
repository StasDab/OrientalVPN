from aiogram import F, Router
from aiogram.types import Message, PreCheckoutQuery

from app.config import settings
from app.db.repositories import (
    create_event,
    create_or_extend_subscription,
    create_payment,
    get_or_create_user,
    get_payment_by_tg_charge,
    update_payment_status,
)
from app.db.session import SessionLocal
from app.plans import PLAN_MAP, decode_invoice_payload, plan_days
from app.services.node_registry import pick_node_for_location
from app.services.retry import with_retry
from app.services.vpn_provider import MarzbanAdapter
from app.telegram_format import subscription_url_pre_block

router = Router()


@router.pre_checkout_query()
async def pre_checkout(pre_checkout_query: PreCheckoutQuery) -> None:
    inv = decode_invoice_payload(pre_checkout_query.invoice_payload or "")
    if not inv:
        await pre_checkout_query.answer(ok=False, error_message="Некорректный счёт.")
        return
    if pre_checkout_query.from_user.id != inv.buyer_tg_id:
        await pre_checkout_query.answer(ok=False, error_message="Плательщик не совпадает с заказом.")
        return
    if (pre_checkout_query.currency or "").upper() != "RUB":
        await pre_checkout_query.answer(ok=False, error_message="Неверная валюта.")
        return

    plan = PLAN_MAP.get(inv.plan_code)
    if not plan or pre_checkout_query.total_amount != plan["amount"]:
        await pre_checkout_query.answer(ok=False, error_message="Сумма не совпадает с тарифом.")
        return

    await pre_checkout_query.answer(ok=True)


@router.message(F.successful_payment)
async def successful_payment(message: Message) -> None:
    if not message.successful_payment or not message.from_user:
        return

    payment = message.successful_payment
    inv = decode_invoice_payload(payment.invoice_payload or "")
    if not inv:
        await message.answer("Ошибка: некорректные данные платежа. Обратитесь в поддержку.")
        return
    if message.from_user.id != inv.buyer_tg_id:
        await message.answer("Ошибка: платёж оформлен не с вашего аккаунта.")
        return

    plan_code = inv.plan_code
    location_code = inv.location_code
    buyer_tg_id = inv.buyer_tg_id
    plan_days_val = plan_days(plan_code)

    async with SessionLocal() as session:
        existing = await get_payment_by_tg_charge(session, payment.telegram_payment_charge_id)
        if existing and existing.status == "paid":
            await message.answer("Платёж уже обработан, доступ ранее выдан.")
            return
        if existing and existing.status == "pending_provision":
            await message.answer(
                "Оплата принята, доступ ещё выдаётся (ожидается свободная нода или восстановление API). "
                "Проверьте /my позже."
            )
            return

        db_user = await get_or_create_user(
            session,
            tg_id=buyer_tg_id,
            username=message.from_user.username,
        )

        pending_row = await create_payment(
            session=session,
            user_id=db_user.id,
            tg_charge_id=payment.telegram_payment_charge_id,
            provider_charge_id=payment.provider_payment_charge_id,
            amount_minor=payment.total_amount,
            currency=payment.currency,
            invoice_payload=payment.invoice_payload,
            status="pending_provision",
        )
        await session.flush()

        selected_node = pick_node_for_location(location_code)
        if not selected_node:
            await create_event(
                session,
                "provision_payment",
                {
                    "payment_id": pending_row.id,
                    "tg_user_id": buyer_tg_id,
                    "plan_code": plan_code,
                    "location_code": location_code,
                    "plan_days": plan_days_val,
                },
            )
            await session.commit()
            await message.answer(
                "Оплата получена, но сейчас нет доступной ноды в выбранной локации. "
                "Выдача доступа повторится автоматически; статус можно смотреть в «Мои подписки» (/my)."
            )
            return

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
            await create_event(
                session,
                "provision_payment",
                {
                    "payment_id": pending_row.id,
                    "tg_user_id": buyer_tg_id,
                    "plan_code": plan_code,
                    "location_code": location_code,
                    "plan_days": plan_days_val,
                },
            )
            await session.commit()
            await message.answer(
                "Оплата получена, но временно не удалось выдать доступ (ошибка панели VPN). "
                "Повторная попытка выполнится автоматически."
            )
            return

        await create_or_extend_subscription(
            session=session,
            user_id=db_user.id,
            external_user_id=result.external_user_id,
            subscription_url=result.subscription_url,
            location_code=location_code,
            node_api_url=selected_node.api_url,
            duration_days=plan_days_val,
        )
        await update_payment_status(session, pending_row.id, "paid")
        await session.commit()

    await message.answer(
        "Оплата подтверждена.\n"
        f"Локация: {location_code.upper()}"
        f"{subscription_url_pre_block(result.subscription_url)}\n"
        "Инструкция: откройте клиент, вставьте ссылку подписки и обновите профиль.",
        parse_mode="HTML",
        disable_web_page_preview=True,
    )
