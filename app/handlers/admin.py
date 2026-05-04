import asyncio

from aiogram import F, Router
from aiogram.filters import Command, StateFilter
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message

from app.config import settings
from app.datetime_util import naive_utc_timestamp
from app.db.repositories import (
    admin_stats_snapshot,
    extend_subscription_days,
    get_user_by_tg_id,
    list_all_user_tg_ids,
    revoke_user_subscriptions,
)
from app.db.session import SessionLocal
from app.services.node_registry import load_nodes
from app.services.vpn_provider import MarzbanAdapter
from app.states import BroadcastStates

router = Router()


def is_admin(tg_id: int | None) -> bool:
    if tg_id is None:
        return False
    return tg_id in settings.admin_id_set


@router.message(Command("stats"))
async def cmd_stats(message: Message) -> None:
    if not is_admin(message.from_user.id):
        await message.answer("Команда доступна только администраторам.")
        return
    async with SessionLocal() as session:
        snap = await admin_stats_snapshot(session)
    vol_rub = snap["payments_30d_volume_minor"] / 100
    text = (
        "Статистика:\n"
        f"• Активных подписок: {snap['active_subscriptions']}\n"
        f"• Оплат за 30 дней: {snap['payments_30d_count']}\n"
        f"• Сумма оплат за 30 дней: {vol_rub:.2f} RUB\n"
        f"• Ожидают выдачи (pending_provision): {snap['pending_provision_payments']}"
    )
    await message.answer(text)


@router.message(Command("servers"))
async def cmd_servers(message: Message) -> None:
    if not is_admin(message.from_user.id):
        await message.answer("Команда доступна только администраторам.")
        return
    nodes = load_nodes()
    if not nodes:
        await message.answer("Ноды не заданы (VPN_NODES_JSON пуст).")
        return
    lines = []
    for n in nodes:
        h = "ok" if n.is_healthy else "degraded"
        load = f"{n.current_load}/{n.capacity}"
        lines.append(f"• [{n.location_code}] {n.api_url} — {h}, нагрузка {load}")
    await message.answer("Серверы:\n" + "\n".join(lines))


@router.message(Command("add_days"))
async def cmd_add_days(message: Message) -> None:
    if not is_admin(message.from_user.id):
        await message.answer("Команда доступна только администраторам.")
        return
    parts = (message.text or "").split()
    if len(parts) != 3:
        await message.answer("Формат: /add_days <tg_id> <days>")
        return
    try:
        tg_target = int(parts[1])
        extra = int(parts[2])
    except ValueError:
        await message.answer("tg_id и days должны быть числами.")
        return
    async with SessionLocal() as session:
        user = await get_user_by_tg_id(session, tg_target)
        if not user:
            await message.answer("Пользователь не найден в базе.")
            return
        sub = await extend_subscription_days(session, user.id, extra)
        if not sub:
            await message.answer("Нет активной подписки для продления.")
            return
        node_url = sub.node_api_url
        ext_id = sub.external_user_id
        ends_ts = naive_utc_timestamp(sub.ends_at)
        await session.commit()

    try:
        provider = MarzbanAdapter(
            panel_url=node_url,
            username=settings.panel_username,
            password=settings.panel_password,
        )
        await provider.set_expire(ext_id, ends_ts)
    except Exception as exc:
        await message.answer(f"БД обновлена, но панель VPN вернула ошибку: {exc!s}")
        return
    await message.answer(f"Продлено на {extra} д. Новое окончание (UTC): {sub.ends_at}")


@router.message(Command("revoke"))
async def cmd_revoke(message: Message) -> None:
    if not is_admin(message.from_user.id):
        await message.answer("Команда доступна только администраторам.")
        return
    parts = (message.text or "").split()
    if len(parts) != 2:
        await message.answer("Формат: /revoke <tg_id>")
        return
    try:
        tg_target = int(parts[1])
    except ValueError:
        await message.answer("tg_id должен быть числом.")
        return
    async with SessionLocal() as session:
        user = await get_user_by_tg_id(session, tg_target)
        if not user:
            await message.answer("Пользователь не найден.")
            return
        subs = await revoke_user_subscriptions(session, user.id)
        await session.commit()
    for s in subs:
        try:
            provider = MarzbanAdapter(
                panel_url=s.node_api_url,
                username=settings.panel_username,
                password=settings.panel_password,
            )
            await provider.disable_access(s.external_user_id)
        except Exception:
            continue
    await message.answer(f"Отозвано подписок в БД: {len(subs)}. Доступ в панели отключён.")


def _bc_confirm_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(text="Отправить всем", callback_data="bc_yes"),
                InlineKeyboardButton(text="Отмена", callback_data="bc_no"),
            ]
        ]
    )


@router.message(Command("cancel"))
async def cmd_cancel(message: Message, state: FSMContext) -> None:
    await state.clear()
    await message.answer("Операция отменена.")


@router.message(Command("broadcast"))
async def cmd_broadcast(message: Message, state: FSMContext) -> None:
    if not is_admin(message.from_user.id):
        await message.answer("Команда доступна только администраторам.")
        return
    await state.set_state(BroadcastStates.entering_text)
    await message.answer(
        "Рассылка: отправьте текст следующим сообщением.\n"
        "Подтверждение будет запрошено кнопками. /cancel — отмена.",
    )


@router.message(StateFilter(BroadcastStates.entering_text), F.text)
async def broadcast_text(message: Message, state: FSMContext) -> None:
    if not is_admin(message.from_user.id):
        await state.clear()
        return
    if message.text.startswith("/"):
        return
    await state.update_data(broadcast_text=message.text)
    await state.set_state(BroadcastStates.confirm)
    await message.answer(
        "Предпросмотр сообщения:\n\n" + message.text,
        reply_markup=_bc_confirm_kb(),
    )


@router.callback_query(StateFilter(BroadcastStates.confirm), F.data == "bc_no")
async def broadcast_cancel_cb(call: CallbackQuery, state: FSMContext) -> None:
    await state.clear()
    await call.message.edit_text("Рассылка отменена.")
    await call.answer()


@router.callback_query(StateFilter(BroadcastStates.confirm), F.data == "bc_yes")
async def broadcast_send_cb(call: CallbackQuery, state: FSMContext) -> None:
    if not is_admin(call.from_user.id):
        await call.answer("Нет доступа", show_alert=True)
        return
    data = await state.get_data()
    text = data.get("broadcast_text")
    if not text:
        await call.answer("Нет текста", show_alert=True)
        return
    await state.clear()
    await call.answer("Запущено…")

    async with SessionLocal() as session:
        tg_ids = await list_all_user_tg_ids(session)

    ok, fail = 0, 0
    for tg_id in tg_ids:
        try:
            await call.bot.send_message(tg_id, text)
            ok += 1
        except Exception:
            fail += 1
        await asyncio.sleep(0.04)

    await call.message.answer(f"Рассылка завершена. Успешно: {ok}, ошибок: {fail}.")
