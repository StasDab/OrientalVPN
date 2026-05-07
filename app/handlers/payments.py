import logging

from aiogram import F, Router
from aiogram.types import CallbackQuery, Message, PreCheckoutQuery

from app.config import settings
from app.db.repositories import (
    create_payment,
    get_or_create_user,
    get_payment_by_tg_charge,
)
from app.db.session import SessionLocal
from app.payment_fulfillment import fulfill_paid_payment_row
from app.plans import PLAN_MAP, decode_invoice_payload
from app.services.yookassa import YookassaError, get_payment as yk_get_payment
from app.telegram_format import subscription_url_pre_block

log = logging.getLogger(__name__)

router = Router()


@router.callback_query(F.data.startswith("yk:"))
async def yookassa_check_callback(call: CallbackQuery) -> None:
    if not settings.use_yookassa:
        await call.answer()
        return
    if not call.from_user:
        await call.answer()
        return
    pay_id = (call.data or "")[3:].strip()
    if not pay_id:
        await call.answer("Некорректный запрос.", show_alert=True)
        return
    try:
        remote = await yk_get_payment(
            shop_id=settings.yookassa_shop_id.strip(),
            secret_key=settings.yookassa_secret_key.strip(),
            payment_id=pay_id,
        )
    except YookassaError:
        log.exception("yookassa_get_payment_failed", extra={"payment_id": pay_id})
        await call.answer("Не удалось связаться с ЮKassa.", show_alert=True)
        return

    status = (remote.get("status") or "").lower()
    if status != "succeeded":
        await call.answer("Оплата ещё не подтверждена. Подождите или завершите оплату.", show_alert=True)
        return

    async with SessionLocal() as session:
        pay_row = await get_payment_by_tg_charge(session, f"yookassa:{pay_id}")
        if not pay_row:
            await call.answer("Платёж не найден в боте.", show_alert=True)
            return
        inv = decode_invoice_payload(pay_row.invoice_payload or "")
        if not inv or inv.buyer_tg_id != call.from_user.id:
            await call.answer("Это не ваш платёж.", show_alert=True)
            return
        outcome = await fulfill_paid_payment_row(session, pay_row, buyer_tg_id=inv.buyer_tg_id)
        await session.commit()

    await call.answer()

    if outcome.ok and outcome.subscription_url:
        await call.message.answer(
            "Оплата подтверждена.\n"
            "Доступ: все серверы (одна подписка)"
            f"{subscription_url_pre_block(outcome.subscription_url)}\n"
            "Инструкция: откройте клиент, вставьте ссылку подписки и обновите профиль.",
            parse_mode="HTML",
            disable_web_page_preview=True,
        )
        return
    if outcome.already_done:
        await call.message.answer("Доступ уже был выдан ранее. Смотрите «Мои подписки» (/my).")
        return
    if outcome.no_node:
        await call.message.answer(
            "Оплата получена, но сейчас нет свободной ноды. Выдача повторится автоматически."
        )
        return
    if outcome.marzban_error:
        await call.message.answer(
            "Оплата получена, временно не удалось выдать доступ. Повтор попытки — автоматически."
        )


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

    buyer_tg_id = inv.buyer_tg_id

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

        outcome = await fulfill_paid_payment_row(session, pending_row, buyer_tg_id=buyer_tg_id)
        await session.commit()

    if outcome.ok and outcome.subscription_url:
        await message.answer(
            "Оплата подтверждена.\n"
            "Доступ: все серверы (одна подписка)"
            f"{subscription_url_pre_block(outcome.subscription_url)}\n"
            "Инструкция: откройте клиент, вставьте ссылку подписки и обновите профиль.",
            parse_mode="HTML",
            disable_web_page_preview=True,
        )
        return
    if outcome.already_done:
        await message.answer("Платёж уже обработан, доступ ранее выдан.")
        return
    if outcome.no_node:
        await message.answer(
            "Оплата получена, но сейчас нет доступной ноды в выбранной локации. "
            "Выдача доступа повторится автоматически; статус можно смотреть в «Мои подписки» (/my)."
        )
        return
    if outcome.marzban_error:
        await message.answer(
            "Оплата получена, но временно не удалось выдать доступ (ошибка панели VPN). "
            "Повторная попытка выполнится автоматически."
        )
