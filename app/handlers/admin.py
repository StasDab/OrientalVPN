import asyncio
import html as html_escape
import logging
from datetime import datetime, timedelta

from aiogram import F, Router
from aiogram.exceptions import TelegramBadRequest
from aiogram.filters import Command, StateFilter
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message
from sqlalchemy.exc import IntegrityError

from app.config import settings
from app.datetime_util import naive_utc_timestamp, utc_now_naive
from app.db.models import Subscription
from app.db.repositories import (
    admin_stats_snapshot,
    count_active_subscriptions_by_node_url,
    count_users_with_referrer,
    create_or_extend_subscription,
    create_promo_code,
    deactivate_promo,
    delete_all_promo_codes,
    delete_all_subscription_rows,
    delete_subscriptions_for_user,
    extend_subscription_days,
    get_or_create_user,
    get_promo_by_code,
    get_user_by_tg_id,
    hard_delete_promo_by_code,
    list_all_subscription_rows,
    list_all_subscription_rows_for_user,
    list_all_user_tg_ids,
    list_promo_codes_admin,
    normalize_promo_code,
    reset_trial_used_for_all_users,
    revoke_user_subscriptions,
)
from app.db.session import SessionLocal
from app.keyboards.main import admin_nav_back_kb, admin_panel_kb
from app.plans import PLAN_MAP, plan_days
from app.services.node_registry import available_location_codes, load_nodes, pick_primary_node
from app.services.retry import with_retry
from app.services.vpn_provider import MarzbanAdapter
from app.telegram_format import subscription_url_pre_block
from app.states import BroadcastStates

log = logging.getLogger(__name__)

router = Router()

ADMIN_HOME_HTML = "🛠️ <b>Панель администратора</b>\n\nВыберите раздел:"


def _parse_promo_trailing(parts: list[str], start: int) -> tuple[dict[str, object], str | None]:
    """Опциональные хвосты для /promo_add и ключи для /promo_set: max_uses, per_user, expires_days, expires_at."""
    extras: dict[str, object] = {}
    i = start
    while i < len(parts):
        key = parts[i].lower()
        if key == "max_uses":
            if i + 1 >= len(parts):
                return {}, "После max_uses укажите число или none."
            vlow = parts[i + 1].lower()
            if vlow in ("none", "unlimited"):
                extras["max_uses"] = None
            else:
                try:
                    mu = int(parts[i + 1])
                except ValueError:
                    return {}, "max_uses должен быть целым числом или none/unlimited."
                if mu < 1:
                    return {}, "max_uses должно быть ≥ 1 или none/unlimited."
                extras["max_uses"] = mu
            i += 2
            continue
        if key in ("per_user", "max_per_user"):
            if i + 1 >= len(parts):
                return {}, "После per_user нужно число ≥ 1."
            try:
                pu = int(parts[i + 1])
            except ValueError:
                return {}, "per_user: целое число."
            if pu < 1:
                return {}, "per_user ≥ 1."
            extras["max_uses_per_user"] = pu
            i += 2
            continue
        if key == "expires_days":
            if i + 1 >= len(parts):
                return {}, "После expires_days нужно число дней."
            try:
                days = int(parts[i + 1])
            except ValueError:
                return {}, "expires_days: целое число."
            if not (1 <= days <= 3650):
                return {}, "expires_days: 1–3650."
            extras["expires_at"] = utc_now_naive() + timedelta(days=days)
            i += 2
            continue
        if key == "expires_at":
            if i + 1 >= len(parts):
                return {}, "После expires_at нужна дата YYYY-MM-DD."
            try:
                d = datetime.strptime(parts[i + 1], "%Y-%m-%d").date()
            except ValueError:
                return {}, "Формат даты: YYYY-MM-DD."
            extras["expires_at"] = datetime(d.year, d.month, d.day)
            i += 2
            continue
        if key == "expires_clear":
            extras["expires_at"] = None
            i += 1
            continue
        return {}, f"Неизвестный параметр «{parts[i]}»."
    return extras, None


async def _marzban_disable_for_subscriptions(subs: list[Subscription]) -> None:
    """Отключить пользователей Marzban по строкам Subscription (ошибки глотаем по одной строке)."""
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
        "<b>Статус</b> (ok/degraded) и «ёмкость / load» — из <code>VPN_NODES_JSON</code>, справочно.\n\n"
        "<b>Подписчиков на узле:</b> по данным бота — сколько активных подписей привязано к этому "
        "<code>node_api_url</code> (панель выдачи). Живые «онлайн»-сессии в Marzban бот здесь не опрашивает.\n"
    )
    for n in sorted(nodes, key=lambda x: x.location_code.lower()):
        h = "ok" if n.is_healthy else "degraded"
        cap = n.capacity
        static_load = n.current_load
        key = _norm_node_url(n.api_url)
        live = counts.get(key, 0)
        lines.append(
            f"• [{html_escape.escape(n.location_code)}] <code>{html_escape.escape(n.api_url)}</code>\n"
            f"  — статус в конфиге: <b>{h}</b>\n"
            f"  — активных клиентов (подписок по БД бота): <b>{live}</b>\n"
            f"  — справочно из JSON: load {static_load} / capacity {cap}"
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
        "<b>Создать:</b>\n"
        "<code>/promo_add percent CODE 15</code> — скидка % с тарифа\n"
        "<code>/promo_add fixed CODE 100</code> — минус сумма в ₽ с тарифа\n"
        "<code>/promo_add days CODE 14</code> — бонус дней при вводе в профиле\n\n"
        "<b>Опции в том же сообщении после основных полей:</b>\n"
        "<code>max_uses N</code> или <code>max_uses none</code>\n"
        "<code>per_user N</code> — активаций на одного человека\n"
        "<code>expires_days N</code> или <code>expires_at YYYY-MM-DD</code>\n\n"
        "<b>Изменить / выключить / удалить:</b>\n"
        "<code>/promo_set CODE max_uses 100 expires_days 30</code>\n"
        "<code>/promo_set CODE expires_clear</code> — снять дату истечения\n"
        "<code>/promo_off CODE</code> — выключить (остаётся в БД)\n"
        "<code>/promo_delete CODE</code> — удалить код и связанные записи\n"
        "<code>/promo_wipe_all YES</code> — удалить все промокоды (подтверждение обязательно)\n\n"
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
        xp = ""
        if pr.expires_at:
            xp = ", до " + pr.expires_at.strftime("%Y-%m-%d %H:%M") + " UTC"
        pu = getattr(pr, "max_uses_per_user", 1)
        buf.append(
            f"\n• <code>{html_escape.escape(pr.code)}</code> ({kind_ru} {extra}) "
            f"— активаций {pr.uses_count}/{mx}, на чел. ≤{pu}{xp}, {act}"
        )
    return "".join(buf)


async def _subs_reset_admin_text() -> str:
    return (
        "📁 <b>Подписки и сброс</b>\n\n"
        "<b>Выдача подписки (Marzban + БД бота, без платежа):</b>\n"
        "<code>/give_sub &lt;tg_id&gt; &lt;тариф&gt;</code> — как при покупке тарифа из «Купить»: "
        "<code>1m</code>, <code>3m</code>, <code>6m</code>, <code>12m</code>. "
        "Продлевает текущую активную подписку или создаёт новую; пользователю уходит личное сообщение со ссылкой (если возможно).\n\n"
        "Остальные команды ниже управляют записями в БД; Marzban отключается там, где указано.\n\n"
        "<b>Через бота:</b>\n"
        "<code>/revoke &lt;tg_id&gt;</code> — активные подписки → статус <code>revoked</code> в БД + "
        "<code>disable</code> в Marzban по каждой записи.\n"
        "<code>/wipe_subs &lt;tg_id&gt;</code> — удалить все строки подписок этого пользователя из БД "
        "(любой статус) + отключить соответствующих пользователей Marzban.\n"
        "<code>/add_days &lt;tg_id&gt; &lt;дней&gt;</code> — продлить активную подписку в БД и обновить expire в панели.\n\n"
        "<b>Массово (опасно, нужно YES):</b>\n"
        "<code>/wipe_all_subs YES</code> — удалить <b>все</b> строки подписок в БД + отключить соответствующих пользователей в Marzban по каждой строке.\n"
        "<code>/reset_trials_all YES</code> — для <b>всех</b> пользователей <code>trial_used = false</code>.\n"
        "<code>/reset_trials_all YES WIPE_SUBS</code> — то же и плюс удалить все подписки в БД (как скрипт <code>--wipe-subs</code>). Marzban для WIPE здесь же отключится по каждой подписке до удаления.\n\n"
        "<b>Скрипты на VPS</b> — из корня <code>/opt/myvpn</code>: "
        "(виртуальное окружение <code>/opt/myvpn/.venv</code>, не из каталога <code>app/</code>):\n"
        "<code>/opt/myvpn/.venv/bin/python scripts/reset_all_trials.py --dry-run</code>\n"
        "<code>/opt/myvpn/.venv/bin/python scripts/reset_all_trials.py --apply</code> — сброс trial у всех\n"
        "<code>/opt/myvpn/.venv/bin/python scripts/reset_all_trials.py --apply --wipe-subs</code> — то же + удалить все подписки\n"
        "<code>.venv/bin/python scripts/reset_trial_for_tg.py &lt;tg_id&gt;</code> — сброс trial + удалить подписки одного\n"
        "<code>.venv/bin/python scripts/wipe_all_subscriptions.py --dry-run|--apply</code> — только подписки у всех, trial не трогать\n"
        "<code>.venv/bin/python scripts/wipe_subscriptions_for_tg.py &lt;tg_id&gt;</code> — только подписки одного\n\n"
        "Подробнее — раздел «Скрипты» в <code>README.md</code>."
    )


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


@router.callback_query(F.data == "admin_subs")
async def callback_admin_subs(call: CallbackQuery) -> None:
    if not is_admin(call.from_user.id):
        await call.answer("Нет доступа", show_alert=True)
        return
    await call.answer()
    await _nav_edit_admin_message(call, await _subs_reset_admin_text(), admin_nav_back_kb())


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

    extras, terr = _parse_promo_trailing(parts, 4)
    if terr:
        await message.answer(terr)
        return

    pk_kwargs: dict = {}
    if "max_uses" in extras:
        pk_kwargs["max_uses"] = extras["max_uses"]
    if "max_uses_per_user" in extras:
        pk_kwargs["max_uses_per_user"] = int(extras["max_uses_per_user"])
    if "expires_at" in extras:
        pk_kwargs["expires_at"] = extras["expires_at"]

    async with SessionLocal() as session:
        try:
            row = await create_promo_code(
                session,
                code=code,
                kind=pk,
                discount_percent=dpct,
                discount_fixed_minor=dfixed,
                bonus_days=bdays,
                **pk_kwargs,
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


@router.message(Command("promo_set"))
async def cmd_promo_set(message: Message) -> None:
    if not is_admin(message.from_user.id):
        await message.answer("Только админы.")
        return
    parts = (message.text or "").split()
    if len(parts) < 3:
        await message.answer(
            "Формат:\n<code>/promo_set CODE max_uses 100 expires_days 30</code>\n"
            "<code>/promo_set CODE expires_at 2026-12-31</code>\n"
            "<code>/promo_set CODE expires_clear</code>",
            parse_mode="HTML",
        )
        return
    code = normalize_promo_code(parts[1])
    extras, terr = _parse_promo_trailing(parts, 2)
    if terr:
        await message.answer(terr)
        return
    if not extras:
        await message.answer("Укажите хотя бы одно поле (max_uses, per_user, expires_days, expires_at, expires_clear).")
        return
    async with SessionLocal() as session:
        pr = await get_promo_by_code(session, code)
        if not pr:
            await message.answer("Промокод не найден.")
            return
        if "max_uses" in extras:
            pr.max_uses = extras["max_uses"]  # type: ignore[assignment]
        if "max_uses_per_user" in extras:
            pr.max_uses_per_user = int(extras["max_uses_per_user"])
        if "expires_at" in extras:
            pr.expires_at = extras["expires_at"]  # type: ignore[assignment]
        await session.commit()
    await message.answer(f"Обновлено: <code>{html_escape.escape(code)}</code>.", parse_mode="HTML")


@router.message(Command("promo_delete"))
async def cmd_promo_delete(message: Message) -> None:
    if not is_admin(message.from_user.id):
        await message.answer("Только админы.")
        return
    parts = (message.text or "").split(maxsplit=1)
    if len(parts) != 2 or not normalize_promo_code(parts[1]):
        await message.answer("Формат: <code>/promo_delete CODE</code>", parse_mode="HTML")
        return
    code = normalize_promo_code(parts[1])
    async with SessionLocal() as session:
        ok = await hard_delete_promo_by_code(session, code)
        await session.commit()
    if ok:
        await message.answer(f"Промокод <code>{html_escape.escape(code)}</code> удалён из БД.", parse_mode="HTML")
    else:
        await message.answer("Код не найден.")


@router.message(Command("promo_wipe_all"))
async def cmd_promo_wipe_all(message: Message) -> None:
    if not is_admin(message.from_user.id):
        await message.answer("Только админы.")
        return
    parts = (message.text or "").split()
    if len(parts) != 2 or parts[1] != "YES":
        await message.answer(
            "Удалятся <b>все</b> промокоды и связанные активации. "
            "Для подтверждения отправьте ровно:\n<code>/promo_wipe_all YES</code>",
            parse_mode="HTML",
        )
        return
    async with SessionLocal() as session:
        n = await delete_all_promo_codes(session)
        await session.commit()
    await message.answer(f"Удалено промокодов из БД: <b>{n}</b>.", parse_mode="HTML")


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
    await _marzban_disable_for_subscriptions(subs)
    await message.answer(f"Отозвано подписок в БД: {len(subs)}. Доступ в панели отключён.")


@router.message(Command("wipe_subs"))
async def cmd_wipe_subs(message: Message) -> None:
    if not is_admin(message.from_user.id):
        await message.answer("Команда доступна только администраторам.")
        return
    parts = (message.text or "").split()
    if len(parts) != 2:
        await message.answer(
            "Удалить все строки подписок пользователя в БД бота и отключить узлы в Marzban.\n"
            "Формат: <code>/wipe_subs &lt;tg_id&gt;</code>",
            parse_mode="HTML",
        )
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
        subs = await list_all_subscription_rows_for_user(session, user.id)
    await _marzban_disable_for_subscriptions(subs)
    async with SessionLocal() as session:
        user = await get_user_by_tg_id(session, tg_target)
        if not user:
            await message.answer("Пользователь пропал из БД между запросами.")
            return
        n = await delete_subscriptions_for_user(session, user.id)
        await session.commit()
    await message.answer(
        f"Удалено записей подписок: <b>{n}</b>. Marzban отключён по {len(subs)} бывшим строкам.",
        parse_mode="HTML",
    )


@router.message(Command("wipe_all_subs"))
async def cmd_wipe_all_subs(message: Message) -> None:
    if not is_admin(message.from_user.id):
        await message.answer("Команда доступна только администраторам.")
        return
    parts = (message.text or "").split()
    if len(parts) != 2 or parts[1] != "YES":
        await message.answer(
            "Удалит <b>все</b> строки <code>subscriptions</code> у всех пользователей и попытается отключить доступ в "
            "Marzban по каждой строке (<code>trial_used</code> не трогается).\n\n"
            "Чтобы выполнить, отправьте ровно:\n<code>/wipe_all_subs YES</code>",
            parse_mode="HTML",
        )
        return
    async with SessionLocal() as session:
        subs = await list_all_subscription_rows(session)
    await _marzban_disable_for_subscriptions(subs)
    async with SessionLocal() as session:
        n = await delete_all_subscription_rows(session)
        await session.commit()
    await message.answer(
        f"Готово. Удалено записей подписок: <b>{n}</b>. Marzban отключался по "
        f"<b>{len(subs)}</b> строкам (ошибки панели в логе — отдельно).",
        parse_mode="HTML",
    )


@router.message(Command("reset_trials_all"))
async def cmd_reset_trials_all(message: Message) -> None:
    if not is_admin(message.from_user.id):
        await message.answer("Команда доступна только администраторам.")
        return
    parts = (message.text or "").split()
    if len(parts) < 2 or parts[1] != "YES":
        await message.answer(
            "Сброс флага <code>trial_used</code> у <b>всех</b> пользователей в БД бота "
            "(как скрипт <code>reset_all_trials.py --apply</code>).\n\n"
            "<b>Варианты подтверждения:</b>\n"
            "<code>/reset_trials_all YES</code>\n"
            "<code>/reset_trials_all YES WIPE_SUBS</code> — дополнительно удалить все подписки в БД и отключить Marzban по ним.",
            parse_mode="HTML",
        )
        return
    wipe_all = len(parts) >= 3 and parts[2].strip().upper() == "WIPE_SUBS"

    if wipe_all:
        async with SessionLocal() as session:
            subs = await list_all_subscription_rows(session)
            nsubs_rows = len(subs)
        await _marzban_disable_for_subscriptions(subs)
        async with SessionLocal() as session:
            ndel = await delete_all_subscription_rows(session)
            n_users = await reset_trial_used_for_all_users(session)
            await session.commit()
        await message.answer(
            f"Готово. Подписей удалено в БД: <b>{ndel}</b> (было строк: {nsubs_rows}). "
            f"Пользователей с обновлённым trial_used: <b>{n_users}</b>.",
            parse_mode="HTML",
        )
        return

    async with SessionLocal() as session:
        n_users = await reset_trial_used_for_all_users(session)
        await session.commit()
    await message.answer(
        f"Готово. У <b>{n_users}</b> записей в <code>users</code> установлено <code>trial_used = false</code>. "
        "Подписки в БД не удалялись.",
        parse_mode="HTML",
    )


@router.message(Command("give_sub"))
async def cmd_give_sub(message: Message) -> None:
    if not is_admin(message.from_user.id):
        await message.answer("Команда доступна только администраторам.")
        return
    parts = (message.text or "").split()
    tariffs = ", ".join(sorted(PLAN_MAP.keys()))
    if len(parts) != 3:
        await message.answer(
            "<b>Выдача подписки пользователю по Telegram ID</b> "
            "(создание или продление в Marzban и запись в БД бота, без оплаты).\n\n"
            f"Формат:\n<code>/give_sub &lt;tg_id&gt; &lt;тариф&gt;</code>\n"
            f"Тарифы: <code>{tariffs}</code>",
            parse_mode="HTML",
        )
        return
    try:
        tg_target = int(parts[1])
    except ValueError:
        await message.answer("tg_id должен быть числом.")
        return
    plan_code = parts[2].strip().lower()
    if plan_code not in PLAN_MAP:
        await message.answer(f"Неизвестный тариф. Используйте: <code>{tariffs}</code>.", parse_mode="HTML")
        return
    days = plan_days(plan_code)

    if not available_location_codes():
        await message.answer("Нет узлов VPN: проверьте <code>VPN_NODES_JSON</code>.", parse_mode="HTML")
        return
    chosen = pick_primary_node()
    if not chosen:
        await message.answer("Нет доступной ноды для выдачи.")
        return
    loc_code = chosen.location_code

    provider = MarzbanAdapter(
        panel_url=chosen.api_url,
        username=settings.panel_username,
        password=settings.panel_password,
    )
    try:
        result = await with_retry(
            lambda: provider.provision_access(
                tg_target,
                loc_code,
                node=chosen,
                days=days,
            ),
            retries=settings.provision_retries,
            base_delay_seconds=1.0,
        )
    except Exception:
        log.exception(
            "admin_give_sub_marzban_failed",
            extra={"tg_id": tg_target, "plan": plan_code},
        )
        await message.answer("Marzban вернул ошибку — подписка не сохранена. Смотрите логи сервиса.")
        return

    async with SessionLocal() as session:
        user = await get_or_create_user(session, tg_id=tg_target, username=None)
        try:
            sub_row = await create_or_extend_subscription(
                session=session,
                user_id=user.id,
                external_user_id=result.external_user_id,
                subscription_url=result.subscription_url,
                location_code="all",
                node_api_url=chosen.api_url,
                duration_days=days,
                panel_ends_at=result.ends_at,
            )
            await session.commit()
        except Exception:
            await session.rollback()
            log.exception("admin_give_sub_db_failed", extra={"tg_id": tg_target, "plan": plan_code})
            await message.answer(
                "Доступ в Marzban выдан, но запись подписки в БД бота не сохранилась — исправьте вручную или повторите.",
            )
            return

    pt = PLAN_MAP[plan_code]["title"]
    adm_reply = (
        f"✅ Выдан тариф <code>{html_escape.escape(plan_code)}</code> "
        f"({html_escape.escape(str(days))} дн.) пользователю <code>{tg_target}</code>.\n"
        f"Окончание (UTC): <code>{sub_row.ends_at}</code>\n"
        f"{subscription_url_pre_block(sub_row.subscription_url)}"
    )
    await message.answer(adm_reply, parse_mode="HTML", disable_web_page_preview=True)

    try:
        await message.bot.send_message(
            chat_id=tg_target,
            text=(
                "✅ Администратор выдал вам подписку.\n"
                f"<b>{html_escape.escape(str(pt))}</b>\n"
                f"{subscription_url_pre_block(sub_row.subscription_url)}\n\n"
                "Ссылку можно снова открыть в «Мои подписки»."
            ),
            parse_mode="HTML",
            disable_web_page_preview=True,
        )
    except Exception:
        log.warning("admin_give_sub_user_dm_failed", extra={"tg_id": tg_target})


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