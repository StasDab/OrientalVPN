import html as html_escape
import logging

from aiogram import F, Router
from aiogram.filters import Command
from aiogram.types import CallbackQuery, LabeledPrice, Message

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
    locations_kb,
    main_menu_kb,
    plans_kb,
    profile_kb,
    subscriptions_back_kb,
    trial_locations_kb,
)
from app.plans import LOCATION_TITLES, PLAN_MAP, plan_days
from app.services.node_registry import available_location_codes, pick_node_for_location
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
    "/my — подписки, /profile — профиль, /help — эта справка."
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
    await call.message.answer(HELP_TEXT)
    await call.answer()


@router.message(Command("buy"))
async def cmd_buy(message: Message) -> None:
    await message.answer(
        tariffs_select_html(),
        reply_markup=plans_kb(),
        parse_mode="HTML",
    )


@router.callback_query(F.data == "buy")
async def callback_buy(call: CallbackQuery) -> None:
    await call.message.answer(
        tariffs_select_html(),
        reply_markup=plans_kb(),
        parse_mode="HTML",
    )
    await call.answer()


@router.callback_query(F.data == "menu_home")
async def callback_menu_home(call: CallbackQuery) -> None:
    await call.message.answer(
        "🏠 Главное меню",
        reply_markup=main_menu_kb(),
    )
    await call.answer()


@router.message(Command("profile"))
async def cmd_profile(message: Message) -> None:
    if not message.from_user:
        return
    u = message.from_user
    uname = f"@{html_escape.escape(u.username)}" if u.username else "—"
    txt = (
        "👤 <b>Профиль</b>\n"
        f"Ваш ID: <code>{u.id}</code>\n"
        f"Username: {uname}\n"
        "<b>Email (для чеков):</b> <i>не указан — добавим после подключения ЮKassa/Telegram Payments</i>\n\n"
        "💰 <b>Баланс:</b> <code>0 ₽</code>\n\n"
        "🌱 <b>Уровень:</b> Новичок\n\n"
        "💡 <i>Пополните баланс через «Купить» или активируйте промокод, когда функция будет доступна.</i>"
    )
    await message.answer(txt, parse_mode="HTML", reply_markup=profile_kb())


@router.callback_query(F.data == "profile")
async def callback_profile(call: CallbackQuery) -> None:
    if not call.from_user:
        await call.answer()
        return
    u = call.from_user
    uname = f"@{html_escape.escape(u.username)}" if u.username else "—"
    txt = (
        "👤 <b>Профиль</b>\n"
        f"Ваш ID: <code>{u.id}</code>\n"
        f"Username: {uname}\n"
        "<b>Email (для чеков):</b> <i>не указан — добавим после подключения ЮKassa/Telegram Payments</i>\n\n"
        "💰 <b>Баланс:</b> <code>0 ₽</code>\n\n"
        "🌱 <b>Уровень:</b> Новичок\n\n"
        "💡 <i>Активируйте промокод или оформите подписку в разделе «Купить».</i>"
    )
    await call.message.answer(txt, parse_mode="HTML", reply_markup=profile_kb())
    await call.answer()


@router.callback_query(F.data == "profile_level")
async def profile_level(call: CallbackQuery) -> None:
    await call.message.answer(
        "🏆 <b>Ваш уровень</b>\nНовичок",
        parse_mode="HTML",
        reply_markup=subscriptions_back_kb(),
    )
    await call.answer()


@router.callback_query(F.data == "profile_balance")
async def profile_balance(call: CallbackQuery) -> None:
    await call.message.answer(
        "💰 <b>Мой баланс</b>\nСейчас: <code>0 ₽</code>\n\n"
        "Подписка VPN оплачивается отдельным счётом в разделе «Купить».",
        parse_mode="HTML",
        reply_markup=subscriptions_back_kb(),
    )
    await call.answer()


@router.callback_query(F.data == "profile_topup")
async def profile_topup(call: CallbackQuery) -> None:
    await call.message.answer(
        tariffs_select_html(),
        parse_mode="HTML",
        reply_markup=plans_kb(),
    )
    await call.answer()


@router.callback_query(F.data == "profile_promo")
async def profile_promo(call: CallbackQuery) -> None:
    await call.message.answer(
        "🎟️ Промокоды скоро появятся в боте. Следите за обновлениями.",
        reply_markup=subscriptions_back_kb(),
    )
    await call.answer()


@router.callback_query(F.data == "jammer_help")
async def jammer_help(call: CallbackQuery) -> None:
    await call.message.answer(
        jammer_bypass_help_html(),
        parse_mode="HTML",
        disable_web_page_preview=True,
        reply_markup=subscriptions_back_kb(),
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
            await call.message.answer(
                "Подписок пока нет. Используйте «Купить» или пробный период.",
                disable_web_page_preview=True,
                reply_markup=subscriptions_back_kb(),
            )
            await call.answer()
            return
        subs = await list_active_subscriptions_for_user(session, user.id)
    if not subs:
        await call.message.answer(
            subscriptions_list_intro_html() + "\n\n<i>Активных подписок нет.</i>",
            parse_mode="HTML",
            disable_web_page_preview=True,
            reply_markup=subscriptions_back_kb(),
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
    await call.message.answer(
        subscriptions_list_intro_html() + "\n\n" + "\n\n".join(lines),
        parse_mode="HTML",
        disable_web_page_preview=True,
        reply_markup=subscriptions_back_kb(),
    )
    await call.answer()


@router.callback_query(F.data.startswith("plan:"))
async def callback_plan(call: CallbackQuery) -> None:
    plan_code = call.data.split(":")[1]
    if not available_location_codes():
        await call.message.answer(NO_VPN_NODES_TEXT)
        await call.answer()
        return
    await call.message.answer("Выберите локацию:", reply_markup=locations_kb(plan_code))
    await call.answer()


@router.callback_query(F.data.startswith("loc:"))
async def callback_location(call: CallbackQuery) -> None:
    _, plan_code, location = call.data.split(":")
    plan = PLAN_MAP.get(plan_code)
    if not plan:
        await call.answer("Тариф не найден", show_alert=True)
        return

    payload = f"{plan_code}:{location}:{call.from_user.id}"
    prices = [LabeledPrice(label=plan["title"], amount=plan["amount"])]

    await call.bot.send_invoice(
        chat_id=call.message.chat.id,
        title=plan["title"],
        description=f"Подписка на VPN, локация {LOCATION_TITLES.get(location, location.upper())}",
        payload=payload,
        provider_token=settings.provider_token,
        currency="RUB",
        prices=prices,
        need_email=False,
        need_name=False,
        need_phone_number=False,
    )
    await call.answer()


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
    if not available_location_codes():
        await call.message.answer(NO_VPN_NODES_TEXT)
        await call.answer()
        return
    await call.message.answer("Выберите локацию для пробного доступа:", reply_markup=trial_locations_kb())
    await call.answer()


@router.message(Command("trial"))
async def cmd_trial(message: Message) -> None:
    async with SessionLocal() as session:
        user = await get_user_by_tg_id(session, message.from_user.id)
        if user and user.trial_used:
            await message.answer("Пробный период уже использован.")
            return
    if not available_location_codes():
        await message.answer(NO_VPN_NODES_TEXT)
        return
    await message.answer("Выберите локацию для пробного доступа:", reply_markup=trial_locations_kb())


@router.callback_query(F.data.startswith("trial_loc:"))
async def callback_trial_location(call: CallbackQuery) -> None:
    if not call.from_user:
        await call.answer()
        return
    location_code = call.data.split(":")[1].lower()
    if location_code not in available_location_codes():
        await call.answer("Эта локация больше не доступна. Откройте меню заново.", show_alert=True)
        return
    selected_node = pick_node_for_location(location_code)
    if not selected_node:
        await call.answer("Нет доступной ноды в этой локации.", show_alert=True)
        return

    async with SessionLocal() as session:
        user = await get_or_create_user(
            session,
            tg_id=call.from_user.id,
            username=call.from_user.username,
        )
        if user.trial_used:
            await call.answer("Пробный период уже использован.", show_alert=True)
            return

        provider = MarzbanAdapter(
            panel_url=selected_node.api_url,
            username=settings.panel_username,
            password=settings.panel_password,
        )
        try:
            result = await with_retry(
                lambda: provider.provision_access(
                    call.from_user.id,
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
                extra={"tg_id": call.from_user.id, "location": location_code},
            )
            await call.answer("Не удалось выдать пробный доступ. Попробуйте позже.", show_alert=True)
            return

        await create_or_extend_subscription(
            session=session,
            user_id=user.id,
            external_user_id=result.external_user_id,
            subscription_url=result.subscription_url,
            location_code=location_code,
            node_api_url=selected_node.api_url,
            duration_hours=settings.trial_hours,
        )
        await mark_trial_used(session, user.id)
        await session.commit()

    hours = settings.trial_hours
    await call.message.answer(
        f"Пробный доступ на ~{hours} ч.\n"
        f"Локация: {LOCATION_TITLES.get(location_code, location_code.upper())}"
        f"{subscription_url_pre_block(result.subscription_url)}\n"
        "Инструкция: откройте клиент, вставьте ссылку подписки и обновите профиль.",
        parse_mode="HTML",
        disable_web_page_preview=True,
    )
    await call.answer()


@router.callback_query(F.data == "my")
async def callback_my(call: CallbackQuery) -> None:
    if not call.from_user:
        await call.answer()
        return
    async with SessionLocal() as session:
        user = await get_user_by_tg_id(session, call.from_user.id)
        if not user:
            await call.message.answer("Подписок пока нет. Используйте «Купить» или пробный период.")
            await call.answer()
            return
        subs = await list_active_subscriptions_for_user(session, user.id)
    if not subs:
        await call.message.answer(
            subscriptions_list_intro_html() + "\n\n<i>Активных подписок нет.</i>",
            parse_mode="HTML",
            disable_web_page_preview=True,
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
    await call.message.answer(
        subscriptions_list_intro_html() + "\n\n" + "\n\n".join(lines),
        parse_mode="HTML",
        disable_web_page_preview=True,
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
