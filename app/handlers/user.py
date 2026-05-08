import html as html_escape
import logging
import re

from aiogram import F, Router
from aiogram.exceptions import TelegramBadRequest
from aiogram.filters import Command
from aiogram.types import CallbackQuery, InlineKeyboardMarkup, LabeledPrice, Message, InlineKeyboardButton

from app.config import settings
from app.db.repositories import (
    create_or_extend_subscription,
    create_payment,
    get_or_create_user,
    get_user_by_tg_id,
    list_active_subscriptions_for_user,
    mark_trial_used,
)
from app.db.session import SessionLocal
from app.keyboards.main import (
    main_menu_kb,
    plans_kb,
    profile_kb,
    subscriptions_back_kb,
)
from app.plans import LOCATION_TITLES, PLAN_MAP
from app.services.node_registry import (
    available_location_codes,
    pick_primary_node,
)
from app.services.retry import with_retry
from app.services.vpn_provider import MarzbanAdapter
from app.services.yookassa import YookassaError, create_redirect_payment
from app.telegram_format import (
    jammer_bypass_help_html,
    subscription_url_pre_block,
    subscription_url_pre_only,
    subscriptions_list_intro_html,
    tariffs_select_html,
)

log = logging.getLogger(__name__)

router = Router()

_TELEGRAM_TEXT_MAX = 4096


def _callback_edit_target(call: CallbackQuery) -> Message | None:
    """Сообщение, которое можно править через edit_text (не InaccessibleMessage)."""
    m = call.message
    return m if isinstance(m, Message) else None


async def ack_callback(
    call: CallbackQuery,
    *,
    text: str | None = None,
    show_alert: bool = False,
) -> None:
    """Один ответ на callback; игнорируем повторный ответ."""
    try:
        await call.answer(text=text, show_alert=show_alert)
    except TelegramBadRequest:
        pass

NO_VPN_NODES_TEXT = (
    "VPN-ноды не настроены: в .env на сервере задайте VPN_NODES_JSON "
    "(список локаций и api_url панели Marzban). Пример в .env.example. "
    "После правки перезапустите сервис бота."
)


HELP_TEXT = (
    "Как подключиться:\n"
    "1) Установите клиент (Happ, v2rayTun, Nekoray и т.п.).\n"
    "2) Добавьте подписку по ссылке из бота.\n"
    "3) Обновите список серверов в клиенте.\n\n"
    "Команды: /buy — тарифы, /trial — пробный доступ, "
    "/my — подписки, /profile — профиль, /help — эта справка.\n\n"
    "Пробный доступ и оплата дают одну ссылку подписки со всеми серверами — она в разделе «Мои подписки»."
)


async def nav_edit(
    message: Message,
    text: str,
    reply_markup: InlineKeyboardMarkup | None = None,
    *,
    parse_mode: str | None = "HTML",
) -> None:
    """Одно сообщение-меню: правим текст и клавиатуру; при ошибке — новое сообщение."""
    if len(text) > _TELEGRAM_TEXT_MAX:
        text = (
            text[: _TELEGRAM_TEXT_MAX - 80]
            + "\n\n<i>…текст сокращён (лимит Telegram 4096 символов).</i>"
        )
    kwargs = dict(
        text=text,
        reply_markup=reply_markup,
        parse_mode=parse_mode,
        disable_web_page_preview=True,
    )
    try:
        await message.edit_text(**kwargs)
    except TelegramBadRequest as e:
        err = (getattr(e, "message", None) or str(e)).lower()
        if "message is not modified" in err or err.strip() == "bad request: message is not modified":
            return
        log.warning("menu_edit_failed_try_answer", extra={"error": str(e)})
        try:
            await message.answer(**kwargs)
        except TelegramBadRequest as e2:
            log.warning("menu_answer_failed_try_plain", extra={"error": str(e2)})
            plain = re.sub(r"<[^>]+>", "", text).strip()[:_TELEGRAM_TEXT_MAX]
            await message.answer(
                plain or "Откройте меню: /start",
                reply_markup=reply_markup,
                disable_web_page_preview=True,
            )


@router.message(Command("start"))
async def cmd_start(message: Message) -> None:
    await message.answer(
        "👋 <b>OrientalVPN</b>\n"
        "Покупка подписки, пробный период и ссылки доступа.",
        reply_markup=main_menu_kb(),
        parse_mode="HTML",
    )


@router.message(Command("help"))
async def cmd_help(message: Message) -> None:
    await message.answer(HELP_TEXT)


@router.callback_query(F.data == "help")
async def callback_help(call: CallbackQuery) -> None:
    msg = _callback_edit_target(call)
    if not msg:
        await ack_callback(call, text="Откройте меню: /start", show_alert=True)
        return
    await ack_callback(call)
    await nav_edit(msg, HELP_TEXT, main_menu_kb(), parse_mode=None)


@router.message(Command("buy"))
async def cmd_buy(message: Message) -> None:
    await message.answer(
        tariffs_select_html(),
        reply_markup=plans_kb(back_to="menu_home", payment_step_back="buy"),
        parse_mode="HTML",
    )


@router.callback_query(F.data == "buy")
async def callback_buy(call: CallbackQuery) -> None:
    msg = _callback_edit_target(call)
    if not msg:
        await ack_callback(call, text="Откройте меню: /start", show_alert=True)
        return
    await ack_callback(call)
    await nav_edit(
        msg,
        tariffs_select_html(),
        plans_kb(back_to="menu_home", payment_step_back="buy"),
    )


@router.callback_query(F.data == "menu_home")
async def callback_menu_home(call: CallbackQuery) -> None:
    msg = _callback_edit_target(call)
    if not msg:
        await ack_callback(call, text="Откройте меню: /start", show_alert=True)
        return
    await ack_callback(call)
    await nav_edit(
        msg,
        "🏠 <b>Главное меню</b>\nВыберите раздел:",
        main_menu_kb(),
    )


def _profile_text(u) -> str:
    uname = f"@{html_escape.escape(u.username)}" if u.username else "—"
    return (
        "👤 <b>Профиль</b>\n"
        f"Ваш ID: <code>{u.id}</code>\n"
        f"Username: {uname}\n"
        "<b>Email (для чеков):</b> <i>не указан — добавим после подключения ЮKassa/Telegram Payments</i>\n\n"
        "💰 <b>Баланс:</b> <code>0 ₽</code>\n\n"
        "💡 <i>«Пополнить баланс» открывает те же тарифы VPN — оплата через Telegram/Payments; "
        "отдельный кошелёк в боте пока не ведётся. Промокоды — позже.</i>"
    )


@router.message(Command("profile"))
async def cmd_profile(message: Message) -> None:
    if not message.from_user:
        return
    await message.answer(
        _profile_text(message.from_user),
        parse_mode="HTML",
        reply_markup=profile_kb(),
    )


@router.callback_query(F.data == "profile")
async def callback_profile(call: CallbackQuery) -> None:
    if not call.from_user:
        await ack_callback(call)
        return
    msg = _callback_edit_target(call)
    if not msg:
        await ack_callback(call, text="Откройте меню: /start", show_alert=True)
        return
    await ack_callback(call)
    await nav_edit(msg, _profile_text(call.from_user), profile_kb())


@router.callback_query(F.data == "profile_balance")
async def profile_balance(call: CallbackQuery) -> None:
    msg = _callback_edit_target(call)
    if not msg:
        await ack_callback(call, text="Откройте меню: /start", show_alert=True)
        return
    await ack_callback(call)
    await nav_edit(
        msg,
        "💰 <b>Мой баланс</b>\nСейчас: <code>0 ₽</code>\n\n"
        "Подписка VPN оплачивается отдельным счётом в разделе «Купить».",
        subscriptions_back_kb(),
    )


@router.callback_query(F.data == "profile_topup")
async def profile_topup(call: CallbackQuery) -> None:
    msg = _callback_edit_target(call)
    if not msg:
        await ack_callback(call, text="Откройте меню: /start", show_alert=True)
        return
    await ack_callback(call)
    await nav_edit(
        msg,
        tariffs_select_html(),
        plans_kb(back_to="profile", payment_step_back="profile"),
    )


@router.callback_query(F.data == "profile_promo")
async def profile_promo(call: CallbackQuery) -> None:
    msg = _callback_edit_target(call)
    if not msg:
        await ack_callback(call, text="Откройте меню: /start", show_alert=True)
        return
    await ack_callback(call)
    await nav_edit(
        msg,
        "🎟️ Промокоды скоро появятся в боте. Следите за обновлениями.",
        subscriptions_back_kb(),
    )


@router.callback_query(F.data == "jammer_help")
async def jammer_help(call: CallbackQuery) -> None:
    msg = _callback_edit_target(call)
    if not msg:
        await ack_callback(call, text="Откройте меню: /start", show_alert=True)
        return
    await ack_callback(call)
    await nav_edit(
        msg,
        jammer_bypass_help_html(),
        subscriptions_back_kb(),
    )


@router.callback_query(F.data == "profile_subs")
async def profile_subs(call: CallbackQuery) -> None:
    if not call.from_user:
        await ack_callback(call)
        return
    msg = _callback_edit_target(call)
    if not msg:
        await ack_callback(call, text="Откройте меню: /start", show_alert=True)
        return
    await ack_callback(call)
    async with SessionLocal() as session:
        user = await get_user_by_tg_id(session, call.from_user.id)
        if not user:
            await nav_edit(
                msg,
                "Подписок пока нет. Используйте «Купить» или пробный период.",
                subscriptions_back_kb(),
                parse_mode=None,
            )
            return
        subs = await list_active_subscriptions_for_user(session, user.id)
    if not subs:
        await nav_edit(
            msg,
            subscriptions_list_intro_html() + "\n\n<i>Активных подписок нет.</i>",
            subscriptions_back_kb(),
        )
        return
    lines = []
    for s in subs:
        loc = LOCATION_TITLES.get(s.location_code, s.location_code.upper())
        lines.append(
            f"<b>{html_escape.escape(loc)}</b> — до {s.ends_at.strftime('%Y-%m-%d %H:%M')} UTC\n"
            f"{subscription_url_pre_only(s.subscription_url)}"
        )
    await nav_edit(
        msg,
        subscriptions_list_intro_html() + "\n\n" + "\n\n".join(lines),
        subscriptions_back_kb(),
    )


@router.callback_query(F.data.startswith("plan:"))
async def callback_plan(call: CallbackQuery) -> None:
    if not call.from_user:
        await ack_callback(call)
        return
    msg = _callback_edit_target(call)
    if not msg:
        await ack_callback(call, text="Откройте меню: /start", show_alert=True)
        return

    parts = (call.data or "").split(":")
    # plan:<code> или plan:<code>:buy|profile
    plan_code = parts[1] if len(parts) > 1 else ""
    payment_step_back = parts[2] if len(parts) > 2 and parts[2] in ("buy", "profile") else "buy"
    if not available_location_codes():
        await ack_callback(call)
        await nav_edit(msg, NO_VPN_NODES_TEXT, main_menu_kb(), parse_mode=None)
        return
    plan = PLAN_MAP.get(plan_code)
    if not plan:
        await ack_callback(call, text="Тариф не найден", show_alert=True)
        return

    await ack_callback(call)
    await nav_edit(
        msg,
        "💳 <b>Оплата</b>\nВыберите способ ниже — доступ ко <b>всем серверам</b> в одной подписке.",
        plans_kb(back_to=payment_step_back, payment_step_back=payment_step_back),
    )

    payload = f"{plan_code}:all:{call.from_user.id}"

    if settings.use_yookassa:
        ret = (settings.yookassa_return_url or "").strip()
        if not ret:
            await call.message.answer(
                "ЮKassa включена, но не задан <code>YOOKASSA_RETURN_URL</code> в .env "
                "(HTTPS, например <code>https://t.me/…</code>).",
                parse_mode="HTML",
            )
            return
        amount_rub = f"{plan['amount'] / 100:.2f}"
        try:
            yk_data = await create_redirect_payment(
                shop_id=settings.yookassa_shop_id.strip(),
                secret_key=settings.yookassa_secret_key.strip(),
                amount_value_rub=amount_rub,
                return_url=ret,
                description=plan["title"][:128],
                metadata={
                    "tg_id": str(call.from_user.id),
                    "plan_code": plan_code,
                    "location_code": "all",
                },
            )
        except YookassaError:
            log.exception("yookassa_create_payment_failed", extra={"tg_id": call.from_user.id})
            await call.message.answer("Не удалось создать платёж ЮKassa. Попробуйте позже.")
            return
        except Exception:
            log.exception("yookassa_create_payment_unexpected", extra={"tg_id": call.from_user.id})
            await call.message.answer("Ошибка оплаты. Попробуйте позже.")
            return

        pay_id = yk_data.get("id") or ""
        conf = yk_data.get("confirmation") or {}
        pay_url = conf.get("confirmation_url") or ""
        if not pay_id or not pay_url:
            await call.message.answer("ЮKassa вернула неполный ответ. Попробуйте позже.")
            return

        async with SessionLocal() as session:
            db_user = await get_or_create_user(
                session,
                tg_id=call.from_user.id,
                username=call.from_user.username,
            )
            await create_payment(
                session=session,
                user_id=db_user.id,
                tg_charge_id=f"yookassa:{pay_id}",
                provider_charge_id=pay_id,
                amount_minor=plan["amount"],
                currency="RUB",
                invoice_payload=payload,
                status="pending_yookassa",
            )
            await session.commit()

        kb = InlineKeyboardMarkup(
            inline_keyboard=[
                [InlineKeyboardButton(text="💳 Оплатить (ЮKassa)", url=pay_url)],
                [InlineKeyboardButton(text="🔄 Проверить оплату", callback_data=f"yk:{pay_id}")],
            ]
        )
        await call.message.answer(
            f"💳 Счёт создан: <b>{html_escape.escape(plan['title'])}</b>\n"
            f"Сумма: <code>{amount_rub} ₽</code>\n\n"
            "Нажмите «Оплатить», завершите оплату на сайте ЮKassa, затем «Проверить оплату».",
            reply_markup=kb,
            parse_mode="HTML",
            disable_web_page_preview=True,
        )
        return

    if not (settings.provider_token or "").strip():
        await call.message.answer(
            "Платежи Telegram не настроены: в .env задайте <code>PROVIDER_TOKEN</code> "
            "(BotFather → бот → Payments, токен после привязки ЮKassa) и перезапустите бота. "
            "Должно быть <code>PAYMENT_PROVIDER=telegram</code>.",
            parse_mode="HTML",
            disable_web_page_preview=True,
        )
        return

    prices = [LabeledPrice(label=plan["title"], amount=plan["amount"])]
    await call.bot.send_invoice(
        chat_id=msg.chat.id,
        title=plan["title"],
        description="OrientalVPN — все доступные серверы в одной подписке",
        payload=payload,
        provider_token=settings.provider_token,
        currency="RUB",
        prices=prices,
        need_email=False,
        need_name=False,
        need_phone_number=False,
    )


async def _run_trial(message: Message, tg_user) -> None:
    async with SessionLocal() as session:
        user = await get_or_create_user(session, tg_id=tg_user.id, username=tg_user.username)
        if user.trial_used:
            await message.answer("Пробный период уже использован.")
            return

    if not available_location_codes():
        await message.answer(NO_VPN_NODES_TEXT)
        return

    selected_node = pick_primary_node()
    if not selected_node:
        await message.answer("Нет доступных серверов в конфигурации.")
        return

    location_code = selected_node.location_code

    async with SessionLocal() as session:
        user = await get_or_create_user(session, tg_id=tg_user.id, username=tg_user.username)
        if user.trial_used:
            await message.answer("Пробный период уже использован.")
            return

        provider = MarzbanAdapter(
            panel_url=selected_node.api_url,
            username=settings.panel_username,
            password=settings.panel_password,
        )
        try:
            result = await with_retry(
                lambda: provider.provision_access(
                    tg_user.id,
                    location_code,
                    node=selected_node,
                    hours=settings.trial_hours,
                ),
                retries=settings.provision_retries,
                base_delay_seconds=1.0,
            )
        except Exception:
            await session.rollback()
            log.exception(
                "trial_provision_failed",
                extra={"tg_id": tg_user.id, "location": location_code},
            )
            await message.answer("Не удалось выдать пробный доступ. Попробуйте позже.")
            return

        sub_row = await create_or_extend_subscription(
            session=session,
            user_id=user.id,
            external_user_id=result.external_user_id,
            subscription_url=result.subscription_url,
            location_code="all",
            node_api_url=selected_node.api_url,
            duration_hours=settings.trial_hours,
            panel_ends_at=result.ends_at,
        )
        await mark_trial_used(session, user.id)
        await session.commit()
        public_sub_url = sub_row.subscription_url

    hours = settings.trial_hours
    await message.answer(
        f"✅ Пробный доступ на <b>~{hours} ч.</b>\n"
        "В подписке сразу <b>все серверы</b>. Ссылка — в «Мои подписки» (/my).\n"
        f"{subscription_url_pre_block(public_sub_url)}\n"
        "Инструкция: клиент → вставить ссылку → обновить профиль.",
        parse_mode="HTML",
        disable_web_page_preview=True,
        reply_markup=main_menu_kb(),
    )


@router.callback_query(F.data == "trial")
async def callback_trial(call: CallbackQuery) -> None:
    if not call.from_user:
        await ack_callback(call)
        return
    msg = _callback_edit_target(call)
    if not msg:
        await ack_callback(call, text="Откройте меню: /start", show_alert=True)
        return
    async with SessionLocal() as session:
        user = await get_user_by_tg_id(session, call.from_user.id)
        if user and user.trial_used:
            await ack_callback(call, text="Пробный период уже использован.", show_alert=True)
            return

    await ack_callback(call)
    await nav_edit(msg, "⏳ Выдаём пробный доступ…", main_menu_kb())
    await _run_trial(msg, call.from_user)


@router.message(Command("trial"))
async def cmd_trial(message: Message) -> None:
    if not message.from_user:
        return
    async with SessionLocal() as session:
        user = await get_user_by_tg_id(session, message.from_user.id)
        if user and user.trial_used:
            await message.answer("Пробный период уже использован.")
            return
    await _run_trial(message, message.from_user)


@router.callback_query(F.data == "srv_menu")
async def callback_srv_menu_legacy(call: CallbackQuery) -> None:
    await ack_callback(call, text="Раздел убран. Откройте «Мои подписки».", show_alert=True)


@router.callback_query(F.data.startswith("srvpick:"))
async def callback_srv_pick_legacy(call: CallbackQuery) -> None:
    await ack_callback(call, text="Используйте «Мои подписки» — одна ссылка на все серверы.", show_alert=True)


@router.callback_query(F.data == "my")
async def callback_my(call: CallbackQuery) -> None:
    if not call.from_user:
        await ack_callback(call)
        return
    msg = _callback_edit_target(call)
    if not msg:
        await ack_callback(call, text="Откройте меню: /start", show_alert=True)
        return
    await ack_callback(call)
    async with SessionLocal() as session:
        user = await get_user_by_tg_id(session, call.from_user.id)
        if not user:
            await nav_edit(
                msg,
                "Подписок пока нет. Используйте «Купить» или пробный период.",
                main_menu_kb(),
                parse_mode=None,
            )
            return
        subs = await list_active_subscriptions_for_user(session, user.id)
    if not subs:
        await nav_edit(
            msg,
            subscriptions_list_intro_html() + "\n\n<i>Активных подписок нет.</i>",
            main_menu_kb(),
        )
        return
    lines = []
    for s in subs:
        loc = LOCATION_TITLES.get(s.location_code, s.location_code.upper())
        lines.append(
            f"<b>{html_escape.escape(loc)}</b> — до {s.ends_at.strftime('%Y-%m-%d %H:%M')} UTC\n"
            f"{subscription_url_pre_only(s.subscription_url)}"
        )
    await nav_edit(
        msg,
        subscriptions_list_intro_html() + "\n\n" + "\n\n".join(lines),
        main_menu_kb(),
    )


@router.message(Command("my"))
async def cmd_my(message: Message) -> None:
    async with SessionLocal() as session:
        user = await get_user_by_tg_id(session, message.from_user.id)
        if not user:
            await message.answer("Подписок пока нет.")
            return
        subs = await list_active_subscriptions_for_user(session, user.id)
    if not subs:
        await message.answer(
            subscriptions_list_intro_html() + "\n\n<i>Активных подписок нет.</i>",
            parse_mode="HTML",
            disable_web_page_preview=True,
        )
        return
    lines = []
    for s in subs:
        loc = LOCATION_TITLES.get(s.location_code, s.location_code.upper())
        lines.append(
            f"<b>{html_escape.escape(loc)}</b> — до {s.ends_at.strftime('%Y-%m-%d %H:%M')} UTC\n"
            f"{subscription_url_pre_only(s.subscription_url)}"
        )
    await message.answer(
        subscriptions_list_intro_html() + "\n\n" + "\n\n".join(lines),
        parse_mode="HTML",
        disable_web_page_preview=True,
    )
