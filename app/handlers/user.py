import html as html_escape
import logging

from aiogram import F, Router
from aiogram.exceptions import TelegramBadRequest
from aiogram.filters import Command
from aiogram.types import CallbackQuery, InlineKeyboardMarkup, LabeledPrice, Message

from app.config import settings
from app.db.repositories import (
    create_or_extend_subscription,
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
    servers_pick_kb,
    subscriptions_back_kb,
)
from app.plans import LOCATION_TITLES, PLAN_MAP
from app.services.node_registry import (
    available_location_codes,
    pick_node_for_location,
    pick_primary_node,
)
from app.services.retry import with_retry
from app.services.vpn_provider import MarzbanAdapter
from app.telegram_format import (
    jammer_bypass_help_html,
    subscription_url_pre_block,
    subscription_url_pre_only,
    subscriptions_list_intro_html,
    tariffs_select_html,
)

log = logging.getLogger(__name__)

router = Router()

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
    "Пробный доступ и оплата дают одну ссылку подписки со всеми серверами. "
    "Отдельную ссылку на один узел можно взять в «Выбрать сервер» (нужен link_match в VPN_NODES_JSON)."
)


async def nav_edit(
    message: Message,
    text: str,
    reply_markup: InlineKeyboardMarkup | None = None,
    *,
    parse_mode: str | None = "HTML",
) -> None:
    """Одно сообщение-меню: правим текст и клавиатуру; при ошибке — новое сообщение."""
    try:
        await message.edit_text(
            text,
            reply_markup=reply_markup,
            parse_mode=parse_mode,
            disable_web_page_preview=True,
        )
    except TelegramBadRequest as e:
        err = (getattr(e, "message", None) or str(e)).lower()
        if "message is not modified" in err:
            return
        if (
            "there is no text" in err
            or "not modified" in err
            or "message can't be edited" in err
            or "can not be edited" in err
        ):
            await message.answer(
                text,
                reply_markup=reply_markup,
                parse_mode=parse_mode,
                disable_web_page_preview=True,
            )
            return
        raise


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
    await nav_edit(call.message, HELP_TEXT, main_menu_kb(), parse_mode=None)
    await call.answer()


@router.message(Command("buy"))
async def cmd_buy(message: Message) -> None:
    await message.answer(
        tariffs_select_html(),
        reply_markup=plans_kb(back_to="menu_home"),
        parse_mode="HTML",
    )


@router.callback_query(F.data == "buy")
async def callback_buy(call: CallbackQuery) -> None:
    await nav_edit(
        call.message,
        tariffs_select_html(),
        plans_kb(back_to="menu_home"),
    )
    await call.answer()


@router.callback_query(F.data == "menu_home")
async def callback_menu_home(call: CallbackQuery) -> None:
    await nav_edit(
        call.message,
        "🏠 <b>Главное меню</b>\nВыберите раздел:",
        main_menu_kb(),
    )
    await call.answer()


def _profile_text(u) -> str:
    uname = f"@{html_escape.escape(u.username)}" if u.username else "—"
    return (
        "👤 <b>Профиль</b>\n"
        f"Ваш ID: <code>{u.id}</code>\n"
        f"Username: {uname}\n"
        "<b>Email (для чеков):</b> <i>не указан — добавим после подключения ЮKassa/Telegram Payments</i>\n\n"
        "💰 <b>Баланс:</b> <code>0 ₽</code>\n\n"
        "💡 <i>Пополните баланс через «Купить» или активируйте промокод, когда функция будет доступна.</i>"
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
        await call.answer()
        return
    await nav_edit(call.message, _profile_text(call.from_user), profile_kb())
    await call.answer()


@router.callback_query(F.data == "profile_balance")
async def profile_balance(call: CallbackQuery) -> None:
    await nav_edit(
        call.message,
        "💰 <b>Мой баланс</b>\nСейчас: <code>0 ₽</code>\n\n"
        "Подписка VPN оплачивается отдельным счётом в разделе «Купить».",
        subscriptions_back_kb(),
    )
    await call.answer()


@router.callback_query(F.data == "profile_topup")
async def profile_topup(call: CallbackQuery) -> None:
    await nav_edit(
        call.message,
        tariffs_select_html(),
        plans_kb(back_to="profile"),
    )
    await call.answer()


@router.callback_query(F.data == "profile_promo")
async def profile_promo(call: CallbackQuery) -> None:
    await nav_edit(
        call.message,
        "🎟️ Промокоды скоро появятся в боте. Следите за обновлениями.",
        subscriptions_back_kb(),
    )
    await call.answer()


@router.callback_query(F.data == "jammer_help")
async def jammer_help(call: CallbackQuery) -> None:
    await nav_edit(
        call.message,
        jammer_bypass_help_html(),
        subscriptions_back_kb(),
    )
    await call.answer()


@router.callback_query(F.data == "profile_subs")
async def profile_subs(call: CallbackQuery) -> None:
    if not call.from_user:
        await call.answer()
        return
    async with SessionLocal() as session:
        user = await get_user_by_tg_id(session, call.from_user.id)
        if not user:
            await nav_edit(
                call.message,
                "Подписок пока нет. Используйте «Купить» или пробный период.",
                subscriptions_back_kb(),
                parse_mode=None,
            )
            await call.answer()
            return
        subs = await list_active_subscriptions_for_user(session, user.id)
    if not subs:
        await nav_edit(
            call.message,
            subscriptions_list_intro_html() + "\n\n<i>Активных подписок нет.</i>",
            subscriptions_back_kb(),
        )
        await call.answer()
        return
    lines = []
    for s in subs:
        loc = LOCATION_TITLES.get(s.location_code, s.location_code.upper())
        lines.append(
            f"<b>{html_escape.escape(loc)}</b> — до {s.ends_at.strftime('%Y-%m-%d %H:%M')} UTC\n"
            f"{subscription_url_pre_only(s.subscription_url)}"
        )
    await nav_edit(
        call.message,
        subscriptions_list_intro_html() + "\n\n" + "\n\n".join(lines),
        subscriptions_back_kb(),
    )
    await call.answer()


@router.callback_query(F.data.startswith("plan:"))
async def callback_plan(call: CallbackQuery) -> None:
    plan_code = call.data.split(":")[1]
    if not available_location_codes():
        await nav_edit(call.message, NO_VPN_NODES_TEXT, main_menu_kb(), parse_mode=None)
        await call.answer()
        return
    plan = PLAN_MAP.get(plan_code)
    if not plan:
        await call.answer("Тариф не найден", show_alert=True)
        return

    await nav_edit(
        call.message,
        "💳 <b>Счёт</b>\nОплатите сообщение со счётом ниже — доступ ко <b>всем серверам</b> в одной подписке.",
        plans_kb(back_to="buy"),
    )

    payload = f"{plan_code}:all:{call.from_user.id}"
    prices = [LabeledPrice(label=plan["title"], amount=plan["amount"])]
    await call.bot.send_invoice(
        chat_id=call.message.chat.id,
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
    await call.answer()


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

        await create_or_extend_subscription(
            session=session,
            user_id=user.id,
            external_user_id=result.external_user_id,
            subscription_url=result.subscription_url,
            location_code="all",
            node_api_url=selected_node.api_url,
            duration_hours=settings.trial_hours,
        )
        await mark_trial_used(session, user.id)
        await session.commit()

    hours = settings.trial_hours
    await message.answer(
        f"✅ Пробный доступ на <b>~{hours} ч.</b>\n"
        "В подписке сразу <b>все серверы</b>. Отдельную ссылку на один узел можно взять в «Выбрать сервер».\n"
        f"{subscription_url_pre_block(result.subscription_url)}\n"
        "Инструкция: клиент → вставить ссылку → обновить профиль.",
        parse_mode="HTML",
        disable_web_page_preview=True,
        reply_markup=main_menu_kb(),
    )


@router.callback_query(F.data == "trial")
async def callback_trial(call: CallbackQuery) -> None:
    if not call.from_user:
        await call.answer()
        return
    async with SessionLocal() as session:
        user = await get_user_by_tg_id(session, call.from_user.id)
        if user and user.trial_used:
            await call.answer("Пробный период уже использован.", show_alert=True)
            return

    await nav_edit(call.message, "⏳ Выдаём пробный доступ…", main_menu_kb())
    await call.answer()
    await _run_trial(call.message, call.from_user)


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
async def callback_srv_menu(call: CallbackQuery) -> None:
    if not call.from_user:
        await call.answer()
        return
    async with SessionLocal() as session:
        user = await get_user_by_tg_id(session, call.from_user.id)
        if not user:
            await nav_edit(
                call.message,
                "Сначала активируйте подписку или пробный период.",
                main_menu_kb(),
                parse_mode=None,
            )
            await call.answer()
            return
        subs = await list_active_subscriptions_for_user(session, user.id)
    if not subs:
        await nav_edit(
            call.message,
            "Нет активной подписки. Используйте «Пробный доступ» или «Купить».",
            main_menu_kb(),
            parse_mode=None,
        )
        await call.answer()
        return

    await nav_edit(
        call.message,
        "🌐 <b>Выберите сервер</b>\n\n"
        "<b>Все серверы</b> — обычная ссылка подписки (/sub/), в клиенте будут все узлы.\n"
        "<b>Один сервер</b> — одна vless-ссылка (в .env для узла задайте "
        "<code>link_match</code>: уникальная подстрока из ссылки, например начало IP).\n",
        servers_pick_kb(),
    )
    await call.answer()


@router.callback_query(F.data.startswith("srvpick:"))
async def callback_srv_pick(call: CallbackQuery) -> None:
    if not call.from_user:
        await call.answer()
        return
    key = call.data.split(":", 1)[1].lower()

    async with SessionLocal() as session:
        user = await get_user_by_tg_id(session, call.from_user.id)
        if not user:
            await call.answer("Нет профиля.", show_alert=True)
            return
        subs = await list_active_subscriptions_for_user(session, user.id)
    if not subs:
        await nav_edit(
            call.message,
            "Нет активной подписки.",
            main_menu_kb(),
            parse_mode=None,
        )
        await call.answer()
        return

    sub = subs[0]
    provider = MarzbanAdapter(
        panel_url=sub.node_api_url,
        username=settings.panel_username,
        password=settings.panel_password,
    )

    if key == "all":
        title = "Все серверы"
        body = (
            f"<b>{html_escape.escape(title)}</b>\n"
            "Импортируйте ссылку — в клиенте появятся все узлы из подписки."
            f"{subscription_url_pre_block(sub.subscription_url)}"
        )
        await nav_edit(
            call.message,
            body,
            servers_pick_kb(),
        )
        await call.answer()
        return

    if key not in available_location_codes():
        await call.answer("Неверный сервер.", show_alert=True)
        return

    node = pick_node_for_location(key)
    if not node:
        await call.answer("Сервер недоступен.", show_alert=True)
        return

    try:
        links = await provider.get_user_share_links(sub.external_user_id)
    except Exception:
        log.exception("fetch_share_links_failed", extra={"user": sub.external_user_id})
        await call.answer("Не удалось получить ссылки с панели.", show_alert=True)
        return

    loc_title = LOCATION_TITLES.get(key, key.upper())
    single_url = ""

    if node.link_match:
        matched = [ln for ln in links if node.link_match in ln]
        if matched:
            single_url = matched[0]

    if not single_url:
        hint = ""
        if not node.link_match:
            hint = (
                "\n\n<i>В VPN_NODES_JSON для этой локации добавьте "
                "<code>\"link_match\":\"&lt;фрагмент IP из ссылки&gt;\"</code> "
                "(уникальный для узла), затем перезапустите бота.</i>"
            )
        body = (
            f"<b>{html_escape.escape(loc_title)}</b>\n"
            "Не удалось выделить одну ссылку автоматически."
            f"{hint}\n\n"
            "<b>Общая подписка (все серверы):</b>"
            f"{subscription_url_pre_block(sub.subscription_url)}"
        )
        await nav_edit(call.message, body, servers_pick_kb())
        await call.answer()
        return

    body = (
        f"<b>{html_escape.escape(loc_title)}</b>\n"
        "Только этот узел (одна vless/vmess ссылка)."
        f"{subscription_url_pre_block(single_url)}\n\n"
        "<b>Все серверы:</b>"
        f"{subscription_url_pre_only(sub.subscription_url)}"
    )
    await nav_edit(call.message, body, servers_pick_kb())
    await call.answer()


@router.callback_query(F.data == "my")
async def callback_my(call: CallbackQuery) -> None:
    if not call.from_user:
        await call.answer()
        return
    async with SessionLocal() as session:
        user = await get_user_by_tg_id(session, call.from_user.id)
        if not user:
            await nav_edit(
                call.message,
                "Подписок пока нет. Используйте «Купить» или пробный период.",
                main_menu_kb(),
                parse_mode=None,
            )
            await call.answer()
            return
        subs = await list_active_subscriptions_for_user(session, user.id)
    if not subs:
        await nav_edit(
            call.message,
            subscriptions_list_intro_html() + "\n\n<i>Активных подписок нет.</i>",
            main_menu_kb(),
        )
        await call.answer()
        return
    lines = []
    for s in subs:
        loc = LOCATION_TITLES.get(s.location_code, s.location_code.upper())
        lines.append(
            f"<b>{html_escape.escape(loc)}</b> — до {s.ends_at.strftime('%Y-%m-%d %H:%M')} UTC\n"
            f"{subscription_url_pre_only(s.subscription_url)}"
        )
    await nav_edit(
        call.message,
        subscriptions_list_intro_html() + "\n\n" + "\n\n".join(lines),
        main_menu_kb(),
    )
    await call.answer()


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
