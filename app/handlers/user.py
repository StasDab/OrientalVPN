import logging
import html

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
    trial_locations_kb,
)
from app.plans import LOCATION_TITLES, PLAN_MAP, plan_days
from app.services.node_registry import available_location_codes, pick_node_for_location
from app.services.retry import with_retry
from app.services.vpn_provider import MarzbanAdapter
from app.telegram_format import subscription_url_pre_block, subscription_url_pre_only

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
    "Команды: /buy — купить, /trial — пробный период, /my — мои подписки, /help — эта справка."
)


@router.message(Command("start"))
async def cmd_start(message: Message) -> None:
    await message.answer(
        "Добро пожаловать. Здесь можно купить VPN или взять короткий пробный доступ.\n"
        "Продолжая, вы принимаете правила сервиса (замените на свою оферту).",
        reply_markup=main_menu_kb(),
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
    await message.answer("Выберите тариф:", reply_markup=plans_kb())


@router.callback_query(F.data == "buy")
async def callback_buy(call: CallbackQuery) -> None:
    await call.message.answer("Выберите тариф:", reply_markup=plans_kb())
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
    fp_hint = ""
    fp = (settings.marzban_reality_fingerprint or "").strip().lower()
    if fp and fp != "chrome":
        fp_hint = (
            f"\n\n<i>Если в Happ после импорта подписки отпечаток всё ещё Chrome: в Marzban откройте "
            f"<b>Host</b> для inbound (например loc-se) и задайте <b>Fingerprint = {html.escape(fp)}</b> "
            "— иначе панель кладёт в ссылку chrome по умолчанию.</i>"
        )
    await call.message.answer(
        f"Пробный доступ на ~{hours} ч.\n"
        f"Локация: {LOCATION_TITLES.get(location_code, location_code.upper())}"
        f"{subscription_url_pre_block(result.subscription_url)}\n"
        "Инструкция: откройте клиент, вставьте ссылку подписки и обновите профиль."
        f"{fp_hint}",
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
        await call.message.answer("Активных подписок нет.")
        await call.answer()
        return
    lines = []
    for s in subs:
        loc = LOCATION_TITLES.get(s.location_code, s.location_code.upper())
        lines.append(
            f"• {loc} — до {s.ends_at.strftime('%Y-%m-%d %H:%M')} UTC\n\n"
            f"{subscription_url_pre_only(s.subscription_url)}"
        )
    await call.message.answer(
        "Ваши активные подписки:\n\n" + "\n\n".join(lines),
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
        await message.answer("Активных подписок нет.")
        return
    lines = []
    for s in subs:
        loc = LOCATION_TITLES.get(s.location_code, s.location_code.upper())
        lines.append(
            f"• {loc} — до {s.ends_at.strftime('%Y-%m-%d %H:%M')} UTC\n\n"
            f"{subscription_url_pre_only(s.subscription_url)}"
        )
    await message.answer(
        "Ваши активные подписки:\n\n" + "\n\n".join(lines),
        parse_mode="HTML",
        disable_web_page_preview=True,
    )
