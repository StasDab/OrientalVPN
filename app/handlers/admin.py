import asyncio
import html as html_escape
import logging

from aiogram import F, Router
from aiogram.exceptions import TelegramBadRequest
from aiogram.filters import Command, StateFilter
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message
from sqlalchemy.exc import IntegrityError

from app.config import settings
from app.datetime_util import naive_utc_timestamp
from app.db.repositories import (
    admin_stats_snapshot,
    count_active_subscriptions_by_node_url,
    count_users_with_referrer,
    create_promo_code,
    deactivate_promo,
    extend_subscription_days,
    get_user_by_tg_id,
    list_all_user_tg_ids,
    list_promo_codes_admin,
    normalize_promo_code,
    revoke_user_subscriptions,
)
from app.db.session import SessionLocal
from app.keyboards.main import admin_nav_back_kb, admin_panel_kb
from app.services.node_registry import load_nodes
from app.services.vpn_provider import MarzbanAdapter
from app.states import BroadcastStates

log = logging.getLogger(__name__)

router = Router()

ADMIN_HOME_HTML = "🛠️ <b>Панель администратора</b>\n\nВыберите раздел:"


def is_admin(tg_id: int | None) -> bool:
    if tg_id is None:
        return False
    return tg_id in settings.admin_id_set


async def _nav_edit_admin_message(
    call: CallbackQuery,
    text: str,
    reply_markup: InlineKeyboardMarkup | None = None,
) -> None:
    if not call.message:
        return
    try:
        await call.message.edit_text(
            text,
            reply_markup=reply_markup,
            parse_mode="HTML",
            disable_web_page_preview=True,
        )
    except TelegramBadRequest as e:
        err = (getattr(e, "message", None) or str(e)).lower()
        if "message is not modified" in err:
            return
        await call.message.answer(text, reply_markup=reply_markup, parse_mode="HTML")


def _norm_node_url(u: str) -> str:
    return (u or "").strip().rstrip("/").lower()


async def _servers_text() -> str:
    nodes = load_nodes()
    if not nodes:
        return "🖥 <b>Серверы</b>\n\nНоды не заданы (<code>VPN_NODES_JSON</code> пуст)."

    counts: dict[str, int] = {}
    async with SessionLocal() as session:
        raw = await count_active_subscriptions_by_node_url(session)
    for k, v in raw.items():
        counts[_norm_node_url(k)] = v

    lines: list[str] = []
    lines.append(
        "🖥 <b>Серверы</b>\n\n"
        "Активные подписки считаются из БД по <code>node_api_url</code> панели.\n"
        "Поля «плановая ёмкость / load» из JSON — справочные, не телеметрия Marzban.\n"
    )
    for n in sorted(nodes, key=lambda x: x.location_code.lower()):
        h = "ok" if n.is_healthy else "degraded"
        cap = n.capacity
        static_load = n.current_load
        key = _norm_node_url(n.api_url)
        live = counts.get(key, 0)
        lines.append(
            f"• [{html_escape.escape(n.location_code)}] <code>{html_escape.escape(n.api_url)}</code>\n"
            f"  — {h}; <b>активных подписок (БД):</b> {live}\n"
            f"  — <i>из JSON:</i> load {static_load} / capacity {cap}"
        )
    return "\n".join(lines)


async def _stats_text() -> str:
    async with SessionLocal() as session:
        snap = await admin_stats_snapshot(session)
    vol_rub = snap["payments_30d_volume_minor"] / 100
    return (
        "📊 <b>Статистика</b>\n\n"
        f"• Активных подписок: {snap['active_subscriptions']}\n"
        f"• Оплат за 30 дней: {snap['payments_30d_count']}\n"
        f"• Сумма оплат за 30 дней: {vol_rub:.2f} RUB\n"
        f"• Ожидают выдачи (pending_provision): {snap['pending_provision_payments']}"
    )


async def _promos_admin_text() -> str:
    async with SessionLocal() as session:
        rows = await list_promo_codes_admin(session, limit=25)
    buf = [
        "🎟️ <b>Промокоды</b>\n\n",
        "Создание (только админы):\n"
        "<code>/promo_add percent CODE 15</code> — 15 % с тарифа\n"
        "<code>/promo_add fixed CODE 100</code> — 100 ₽ с тарифа (в рублях)\n"
        "<code>/promo_add days CODE 14</code> — 14 дн. доступа при вводе в профиле\n"
        "<code>/promo_off CODE</code> — выключить\n\n",
        "<b>Последние промокоды:</b>",
    ]
    if not rows:
        buf.append("\n<i>Пока пусто.</i>")
        return "".join(buf)
    for pr in rows:
        kind_ru = {"percent": "%", "fixed": "₽", "free_days": "дн."}.get(pr.kind, pr.kind)
        extra = ""
        if pr.kind == "percent":
            extra = f"{pr.discount_percent}%"
        elif pr.kind == "fixed":
            extra = f"{(pr.discount_fixed_minor or 0) / 100:.0f} ₽"
        else:
            extra = f"{pr.bonus_days or 0} дн."
        mx = pr.max_uses if pr.max_uses is not None else "∞"
        act = "вкл" if pr.is_active else "выкл"
        buf.append(
            f"\n• <code>{html_escape.escape(pr.code)}</code> ({kind_ru} {extra}) "
            f"— {pr.uses_count}/{mx}, {act}"
        )
    return "".join(buf)


async def _referrals_admin_text() -> str:
    async with SessionLocal() as session:
        with_ref = await count_users_with_referrer(session)
        total_u = len(await list_all_user_tg_ids(session))
    bps = int(settings.referral_commission_bps)
    # Расписанный процент для текста UI: сумма платежа × bps ÷ 10000; для отображения «%» здесь делим только на 100.
    pct = bps / 100
    return (
        "🤝 <b>Рефералы</b>\n\n"
        f"Начисление при покупке подписки: <code>{pct:.2f}%</code> суммы платежа (после скидки) "
        f"на баланс пригласившему. Настройка: <code>REFERRAL_COMMISSION_BPS</code> "
        f"(сейчас <code>{bps}</code>; <code>10000</code> = 100 %).\n\n"
        f"Пользователей с пригласившим в БД: <b>{with_ref}</b>.\n"
        f"Учётных записей всего (для масштаба): <b>{total_u}</b>.\n\n"
        "<b>Ссылка для приглашения:</b>\n"
        "<code>https://t.me/ВАШ_БОТ?start=ref_ИХ_TELEGRAM_ID</code>\n"
        "(подставьте username из BotFather и свой числовой Telegram ID)."
    )


@router.callback_query(F.data == "admin_home")
async def callback_admin_home(call: CallbackQuery, state: FSMContext) -> None:
    if not is_admin(call.from_user.id):
        await call.answer("Нет доступа", show_alert=True)
        return
    await state.clear()
    await call.answer()
    await _nav_edit_admin_message(call, ADMIN_HOME_HTML, admin_panel_kb())


@router.message(Command("admin"))
async def cmd_admin_panel(message: Message) -> None:
    if not is_admin(message.from_user.id):
        await message.answer("Команда доступна только администраторам.")
        return
    await message.answer(ADMIN_HOME_HTML, reply_markup=admin_panel_kb(), parse_mode="HTML")


@router.callback_query(F.data == "admin_panel")
async def callback_admin_panel(call: CallbackQuery, state: FSMContext) -> None:
    if not is_admin(call.from_user.id):
        await call.answer("Нет доступа", show_alert=True)
        return
    await state.clear()
    await call.answer()
    await _nav_edit_admin_message(call, ADMIN_HOME_HTML, admin_panel_kb())


@router.callback_query(F.data == "admin_stats")
async def callback_admin_stats(call: CallbackQuery) -> None:
    if not is_admin(call.from_user.id):
        await call.answer("Нет доступа", show_alert=True)
        return
    await call.answer()
    await _nav_edit_admin_message(
        call,
        await _stats_text(),
        admin_nav_back_kb(),
    )


@router.callback_query(F.data == "admin_servers")
async def callback_admin_servers(call: CallbackQuery) -> None:
    if not is_admin(call.from_user.id):
        await call.answer("Нет доступа", show_alert=True)
        return
    await call.answer()
    await _nav_edit_admin_message(call, await _servers_text(), admin_nav_back_kb())


@router.callback_query(F.data == "admin_promos")
async def callback_admin_promos(call: CallbackQuery) -> None:
    if not is_admin(call.from_user.id):
        await call.answer("Нет доступа", show_alert=True)
        return
    await call.answer()
    await _nav_edit_admin_message(call, await _promos_admin_text(), admin_nav_back_kb())


@router.callback_query(F.data == "admin_referrals")
async def callback_admin_referrals(call: CallbackQuery) -> None:
    if not is_admin(call.from_user.id):
        await call.answer("Нет доступа", show_alert=True)
        return
    await call.answer()
    await _nav_edit_admin_message(call, await _referrals_admin_text(), admin_nav_back_kb())


@router.callback_query(F.data == "admin_broadcast")
async def callback_admin_broadcast(call: CallbackQuery, state: FSMContext) -> None:
    if not is_admin(call.from_user.id):
        await call.answer("Нет доступа", show_alert=True)
        return
    await state.set_state(BroadcastStates.entering_text)
    await call.answer()
    await _nav_edit_admin_message(
        call,
        "📣 <b>Рассылка</b>\n\nОтправьте текст <b>следующим сообщением</b> "
        "(в этом чате).\nПодтверждение будет кнопками.\n"
        "<code>/cancel</code> — отмена. Кнопка «В панель» тоже сбросит режим после подтверждения экранов.",
        admin_nav_back_kb(),
    )


@router.message(Command("stats"))
async def cmd_stats(message: Message) -> None:
    if not is_admin(message.from_user.id):
        await message.answer("Команда доступна только администраторам.")
        return
    await message.answer(await _stats_text(), parse_mode="HTML")


@router.message(Command("servers"))
async def cmd_servers(message: Message) -> None:
    if not is_admin(message.from_user.id):
        await message.answer("Команда доступна только администраторам.")
        return
    await message.answer(await _servers_text(), parse_mode="HTML")


@router.message(Command("promo_add"))
async def cmd_promo_add(message: Message) -> None:
    if not is_admin(message.from_user.id):
        await message.answer("Только админы.")
        return
    parts = (message.text or "").split()
    # /promo_add percent SUMMER15 15
    if len(parts) < 4:
        await message.answer(
            "Формат:\n<code>/promo_add percent CODE 15</code>\n"
            "<code>/promo_add fixed CODE 50</code> — скидка 50 ₽\n<code>/promo_add days WEEK 7</code>",
            parse_mode="HTML",
        )
        return
    kind = parts[1].strip().lower()
    code_raw = parts[2]
    val_raw = parts[3]
    code = normalize_promo_code(code_raw)
    try:
        v = int(val_raw)
    except ValueError:
        await message.answer("Числовой параметр (процент, рубли или дни) задан некорректно.")
        return
    dpct: int | None = None
    dfixed: int | None = None
    bdays: int | None = None
    pk = ""
    if kind == "percent":
        if not (1 <= v <= 99):
            await message.answer("Процент должен быть от 1 до 99.")
            return
        dpct = v
        pk = "percent"
    elif kind in ("fixed", "rub"):
        if v <= 0 or v > 1_000_000:
            await message.answer("Сумма в рублях должна быть в разумных пределах.")
            return
        dfixed = v * 100
        pk = "fixed"
    elif kind == "days":
        if not (1 <= v <= 3650):
            await message.answer("Количество дней должно быть 1–3650.")
            return
        bdays = v
        pk = "free_days"
    else:
        await message.answer("Неизвестный тип. Используйте percent, fixed или days.")
        return

    async with SessionLocal() as session:
        try:
            row = await create_promo_code(
                session,
                code=code,
                kind=pk,
                discount_percent=dpct,
                discount_fixed_minor=dfixed,
                bonus_days=bdays,
            )
            await session.commit()
        except IntegrityError:
            await session.rollback()
            await message.answer("Такой код уже существует (уникальный в БД).")
            log.warning("promo_duplicate", extra={"code": code})
            return
        await message.answer(f"Готово: промокод <code>{html_escape.escape(row.code)}</code>.", parse_mode="HTML")


@router.message(Command("promo_off"))
async def cmd_promo_off(message: Message) -> None:
    if not is_admin(message.from_user.id):
        await message.answer("Только админы.")
        return
    parts = (message.text or "").split(maxsplit=1)
    if len(parts) != 2 or not normalize_promo_code(parts[1]):
        await message.answer("Формат: <code>/promo_off CODE</code>", parse_mode="HTML")
        return
    code = normalize_promo_code(parts[1])
    async with SessionLocal() as session:
        ok = await deactivate_promo(session, code)
        await session.commit()
    if ok:
        await message.answer(f"Промокод <code>{html_escape.escape(code)}</code> выключен.", parse_mode="HTML")
    else:
        await message.answer("Код не найден или уже изменён.")


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
            ],
            [InlineKeyboardButton(text="🔙 В панель", callback_data="admin_home")],
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
        "📣 Рассылка: отправьте текст следующим сообщением.\n"
        "<code>/cancel</code> — отмена.",
        reply_markup=admin_nav_back_kb(),
        parse_mode="HTML",
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
    await call.answer()
    if is_admin(call.from_user.id if call.from_user else None):
        await _nav_edit_admin_message(call, ADMIN_HOME_HTML, admin_panel_kb())
    else:
        try:
            await call.message.edit_text("Рассылка отменена.")
        except TelegramBadRequest:
            pass


@router.callback_query(StateFilter(BroadcastStates.confirm), F.data == "admin_home")
async def broadcast_abandon_to_home(call: CallbackQuery, state: FSMContext) -> None:
    await state.clear()
    await call.answer()
    await _nav_edit_admin_message(call, ADMIN_HOME_HTML, admin_panel_kb())


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

    try:
        await _nav_edit_admin_message(
            call,
            ADMIN_HOME_HTML
            + f"\n\n<i>Рассылка завершена. Успешно: {ok}, ошибок: {fail}.</i>",
            admin_panel_kb(),
        )
    except TelegramBadRequest:
        await call.message.answer(f"Рассылка завершена. Успешно: {ok}, ошибок: {fail}.")