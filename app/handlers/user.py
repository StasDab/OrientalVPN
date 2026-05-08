import html as html_escape
import logging
import re
from pathlib import Path
from uuid import uuid4

from aiogram import Bot, F, Router
from aiogram.dispatcher.event.bases import SkipHandler
from aiogram.enums import ContentType
from aiogram.exceptions import TelegramBadRequest
from aiogram.filters import Command, CommandObject, StateFilter
from aiogram.fsm.context import FSMContext
from aiogram.types import (
    CallbackQuery,
    FSInputFile,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    LabeledPrice,
    Message,
)

from app.config import settings
from app.datetime_util import naive_utc_from_timestamp, utc_now_naive
from app.db.models import Subscription, User
from app.db.repositories import (
    count_referrals_registered,
    count_referrals_with_tariff_payment,
    create_or_extend_subscription,
    create_payment,
    get_or_create_user,
    get_promo_by_code,
    get_promo_by_id,
    get_user_by_tg_id,
    list_active_subscriptions_for_user,
    mark_trial_used,
    normalize_promo_code,
    promo_discount_on_amount,
    record_free_days_promo_redemption,
    set_user_active_promo,
    validate_discount_promo_for_checkout,
    validate_promo_for_apply,
)
from app.db.session import SessionLocal
from app.keyboards.main import (
    main_menu_kb,
    plans_kb,
    profile_kb,
    profile_only_back_kb,
    promo_cancel_kb,
    subscriptions_back_kb,
    topup_cancel_kb,
)
from app.payment_fulfillment import fulfill_paid_payment_row
from app.plans import (
    LOCATION_TITLES,
    PLAN_MAP,
    encode_plan_invoice_payload,
    format_topup_payload,
)
from app.services.node_registry import (
    available_location_codes,
    pick_primary_node,
)
from app.services.retry import with_retry
from app.services.vpn_provider import MarzbanAdapter
from app.services.yookassa import YookassaError, create_redirect_payment
from app.states import PromoStates, TopupStates
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
MIN_TOPUP_RUB = 1
MAX_TOPUP_RUB = 500_000


def _effective_min_topup_rub() -> int:
    """ЮKassa можно с 1 ₽; Telegram Payments режет малые суммы — см. TELEGRAM_MIN_INVOICE_RUB."""
    if settings.use_yookassa:
        return MIN_TOPUP_RUB
    return max(MIN_TOPUP_RUB, int(settings.telegram_min_invoice_rub))


def _user_text_for_invoice_bad_request(exc: TelegramBadRequest) -> str:
    api = ((getattr(exc, "message", None) or str(exc))).upper()
    if "CURRENCY_TOTAL_AMOUNT_INVALID" in api:
        return (
            f"Платёжная система не принимает слишком маленькую сумму. "
            f"Укажите не менее {_effective_min_topup_rub()} ₽ или попробуйте другую сумму."
        )
    return "Не удалось открыть оплату. Попробуйте позже или выберите другую сумму."


def _is_admin_user(tg_id: int | None) -> bool:
    return tg_id is not None and tg_id in settings.admin_id_set


def _deep_link_ref_tg_id(args: str | None) -> int | None:
    raw = (args or "").strip()
    if not raw.startswith("ref_"):
        return None
    suffix = raw[4:].strip()
    try:
        v = int(suffix)
    except ValueError:
        return None
    return v if v > 0 else None


async def _profile_discount_promo_sentence(session: object, db_user: User) -> str:
    if not db_user.active_promo_id:
        return ""
    promo = await get_promo_by_id(session, db_user.active_promo_id)
    if not promo or promo.kind not in ("percent", "fixed"):
        return ""
    code_esc = html_escape.escape(promo.code)
    if promo.kind == "percent":
        return f"\n🎟️ Промокод <code>{code_esc}</code> — скидка {promo.discount_percent}% на тариф.\n"
    fm = promo.discount_fixed_minor or 0
    return f"\n🎟️ Промокод <code>{code_esc}</code> — −{fm / 100:.2f} ₽ от цены тарифа.\n"


def _callback_edit_target(call: CallbackQuery) -> Message | None:
    m = call.message
    return m if isinstance(m, Message) else None


async def ack_callback(
    call: CallbackQuery,
    *,
    text: str | None = None,
    show_alert: bool = False,
) -> None:
    try:
        await call.answer(text=text, show_alert=show_alert)
    except TelegramBadRequest:
        pass


NO_VPN_NODES_TEXT = (
    "VPN-ноды не настроены: в .env на сервере задайте VPN_NODES_JSON "
    "(список локаций и api_url панели Marzban). Пример в .env.example. "
    "После правки перезапустите сервис бота."
)

START_WELCOME_HTML = (
    "👋 <b>Добро пожаловать в OrientalVPN!</b>\n\n"
    "✨ Твой личный ключ к безграничному интернету:\n\n"
    "💎 <b>Высокоскоростной и Надежный VPN:</b>\n"
    "• ⚡️Мгновенная загрузка 4K-видео и стабильный стриминг.\n"
    "• 🛡 Обход Глушилок (DPI): Используем технологию Stealth для надежного доступа в любых условиях.\n"
    "• 🌍 Открытие всех заблокированных ресурсов: Instagram, TikTok, Netflix, Discord.\n"
    "• 🔒 Полная анонимность и защита трафика."
)

HELP_TEXT = (
    "Как подключиться:\n"
    "1) Установите клиент (Happ, v2rayTun, Nekoray и т.п.).\n"
    "2) Добавьте подписку по ссылке из бота.\n"
    "3) Обновите список серверов в клиенте.\n\n"
    "Команды: /buy — тарифы, /trial — пробный доступ, "
    "/my — подписки, /profile — профиль, /help — эта справка.\n\n"
    "«Купить» — оформление подписки: к оплате будет сумма тарифа минус ваш баланс.\n"
    "«Пополнить баланс» в профиле — введите сумму в рублях; пополненный баланс учтётся в счёте подписки.\n\n"
    "В профиле доступны промокоды (скидка при оплате или бесплатные дни) и ваш реферальный запуск через "
    "ссылку вида «старт со ссылкой» от бота (подробности там же).\n\n"
    "Пробный доступ и оплата дают одну ссылку подписки со всеми серверами — она в разделе «Мои подписки»."
)


def _welcome_main_kb(tg_uid: int | None) -> InlineKeyboardMarkup:
    return main_menu_kb(is_admin=_is_admin_user(tg_uid))


async def reply_welcome_from_user_msg(message: Message) -> None:
    """Приветственный экран после /start или аналога: баннер + подпись, либо только текст."""
    uid = message.from_user.id if message.from_user else None
    kb = _welcome_main_kb(uid)
    banner_url = (settings.start_banner_url or "").strip()
    banner_path_raw = (settings.start_banner_path or "").strip()

    if banner_path_raw:
        p = Path(banner_path_raw)
        if not p.is_absolute():
            p = (Path.cwd() / p).resolve()
        if p.is_file():
            await message.answer_photo(
                photo=FSInputFile(str(p)),
                caption=START_WELCOME_HTML,
                parse_mode="HTML",
                reply_markup=kb,
            )
            return
    if banner_url:
        await message.answer_photo(
            photo=banner_url,
            caption=START_WELCOME_HTML,
            parse_mode="HTML",
            reply_markup=kb,
        )
        return
    await message.answer(START_WELCOME_HTML, reply_markup=kb, parse_mode="HTML")


async def send_welcome_banner_new_message(bot: Bot, chat_id: int, *, tg_uid: int | None) -> None:
    """Новое сообщение с приветствием (сообщение-инвойс нельзя отредактировать в текст главной)."""
    kb = _welcome_main_kb(tg_uid)
    banner_url = (settings.start_banner_url or "").strip()
    banner_path_raw = (settings.start_banner_path or "").strip()

    if banner_path_raw:
        p = Path(banner_path_raw)
        if not p.is_absolute():
            p = (Path.cwd() / p).resolve()
        if p.is_file():
            await bot.send_photo(
                chat_id=chat_id,
                photo=FSInputFile(str(p)),
                caption=START_WELCOME_HTML,
                parse_mode="HTML",
                reply_markup=kb,
            )
            return
    if banner_url:
        await bot.send_photo(
            chat_id=chat_id,
            photo=banner_url,
            caption=START_WELCOME_HTML,
            parse_mode="HTML",
            reply_markup=kb,
        )
        return
    await bot.send_message(chat_id=chat_id, text=START_WELCOME_HTML, parse_mode="HTML", reply_markup=kb)


def payment_cancel_to_main_kb() -> InlineKeyboardMarkup:
    """
    Клавиатура к сообщению-инвойсу Telegram Payments.
    Нельзя передавать только «Отменить»: API вернёт REPLY_MARKUP_BUY_EMPTY —
    нужна кнопка оплаты (pay=True), либо не передавать reply_markup вообще.
    """
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="Оплатить", pay=True)],
            [InlineKeyboardButton(text="❌ Отменить", callback_data="menu_home")],
        ]
    )


def referral_deeplink(tg_uid: int) -> str:
    uname = settings.public_bot_username.strip().lstrip("@")
    return f"https://t.me/{uname}?start=ref_{int(tg_uid)}"


async def nav_edit(
    message: Message,
    text: str,
    reply_markup: InlineKeyboardMarkup | None = None,
    *,
    parse_mode: str | None = "HTML",
) -> Message:
    """
    Один «экран» меню: для сообщения с фото/инвойсом и т.п. нельзя edit_text —
    удаляем и шлём текстовое сообщение, чтобы инлайн-кнопки оставались рабочими.
    """
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
    bot = message.bot
    chat_id = message.chat.id

    if message.content_type != ContentType.TEXT:
        try:
            await message.delete()
        except TelegramBadRequest:
            pass
        return await bot.send_message(chat_id=chat_id, **kwargs)

    try:
        await message.edit_text(**kwargs)
        return message
    except TelegramBadRequest as e:
        err = (getattr(e, "message", None) or str(e)).lower()
        if "message is not modified" in err or err.strip() == "bad request: message is not modified":
            return message
        log.warning("menu_edit_failed_try_answer", extra={"error": str(e)})
        try:
            return await message.answer(**kwargs)
        except TelegramBadRequest as e2:
            log.warning("menu_answer_failed_try_plain", extra={"error": str(e2)})
            plain = re.sub(r"<[^>]+>", "", text).strip()[:_TELEGRAM_TEXT_MAX]
            return await message.answer(
                plain or "Откройте меню: /start",
                reply_markup=reply_markup,
                disable_web_page_preview=True,
            )


def _profile_text(db_user: User, display_username: str | None, *, promo_sentence: str = "") -> str:
    uname = f"@{html_escape.escape(display_username)}" if display_username else "—"
    bal = db_user.balance_minor / 100
    return (
        "👤 <b>Профиль</b>\n"
        f"Telegram ID: <code>{db_user.tg_id}</code>\n"
        f"Username: {uname}\n\n"
        f"💰 <b>Баланс:</b> <code>{bal:.2f} ₽</code>"
        f"{promo_sentence}\n\n"
        "При пополнении баланса вы указываете желаемую сумму, "
        "которая затем будет использована в счете оплаты подписки."
    )


async def _fetch_marzban_status_line(api_url: str, external_user_id: str) -> str:
    try:
        provider = MarzbanAdapter(
            panel_url=api_url,
            username=settings.panel_username,
            password=settings.panel_password,
        )
        ex = await provider.get_user_expire_unix(external_user_id)
        if ex is None:
            return "Осталось — нет данных (пользователь не найден в панели)."
        if ex == 0:
            return "Осталось — без ограничения срока."
        dt = naive_utc_from_timestamp(ex)
        now = utc_now_naive()
        if dt <= now:
            return "Осталось — срок истёк."
        left = dt - now
        d, h = left.days, left.seconds // 3600
        return f"Осталось — <b>{d}</b> дн. <b>{h}</b> ч."
    except Exception:
        log.warning("marzban_status_fetch_failed", exc_info=True)
        return "Осталось — временно недоступно."


async def _build_subscription_lines(subs: list[Subscription]) -> list[str]:
    cache: dict[tuple[str, str], str] = {}
    lines: list[str] = []
    for s in subs:
        key = (s.node_api_url, s.external_user_id)
        if key not in cache:
            cache[key] = await _fetch_marzban_status_line(key[0], key[1])
        loc = LOCATION_TITLES.get(s.location_code, s.location_code.upper())
        lines.append(
            f"<b>{html_escape.escape(loc)}</b>\n"
            f"<i>{cache[key]}</i>\n"
            f"{subscription_url_pre_only(s.subscription_url)}"
        )
    return lines


@router.message(Command("start"))
async def cmd_start(message: Message, command: CommandObject) -> None:
    if message.from_user:
        ref_tg = _deep_link_ref_tg_id(command.args)
        async with SessionLocal() as session:
            await get_or_create_user(
                session,
                tg_id=message.from_user.id,
                username=message.from_user.username,
                referrer_tg_id=ref_tg,
            )
            await session.commit()
    await reply_welcome_from_user_msg(message)


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
    await nav_edit(
        msg,
        HELP_TEXT,
        main_menu_kb(is_admin=_is_admin_user(call.from_user.id if call.from_user else None)),
        parse_mode=None,
    )


@router.message(Command("buy"))
async def cmd_buy(message: Message) -> None:
    if not message.from_user:
        return
    async with SessionLocal() as session:
        db_user = await get_or_create_user(
            session,
            tg_id=message.from_user.id,
            username=message.from_user.username,
        )
        bal_txt = f"{db_user.balance_minor / 100:.2f} ₽"
        await session.commit()
    await message.answer(
        tariffs_select_html(bal_txt),
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
    async with SessionLocal() as session:
        if not call.from_user:
            return
        db_user = await get_or_create_user(
            session,
            tg_id=call.from_user.id,
            username=call.from_user.username,
        )
        bal_txt = f"{db_user.balance_minor / 100:.2f} ₽"
        await session.commit()
    await nav_edit(
        msg,
        tariffs_select_html(bal_txt),
        plans_kb(back_to="menu_home", payment_step_back="buy"),
    )


@router.callback_query(F.data == "menu_home")
async def callback_menu_home(call: CallbackQuery, state: FSMContext) -> None:
    await state.clear()
    msg = _callback_edit_target(call)
    if not msg:
        await ack_callback(call, text="Откройте меню: /start", show_alert=True)
        return
    await ack_callback(call)
    tg_uid = call.from_user.id if call.from_user else None
    kb = _welcome_main_kb(tg_uid)
    if msg.content_type == ContentType.INVOICE:
        await send_welcome_banner_new_message(call.bot, msg.chat.id, tg_uid=tg_uid)
        return
    await nav_edit(msg, START_WELCOME_HTML, kb)


@router.message(Command("profile"))
async def cmd_profile(message: Message) -> None:
    if not message.from_user:
        return
    async with SessionLocal() as session:
        u = await get_or_create_user(
            session,
            tg_id=message.from_user.id,
            username=message.from_user.username,
        )
        promo_s = await _profile_discount_promo_sentence(session, u)
        await session.commit()
    await message.answer(
        _profile_text(u, message.from_user.username, promo_sentence=promo_s),
        parse_mode="HTML",
        reply_markup=profile_kb(),
    )


@router.callback_query(F.data == "profile")
async def callback_profile(call: CallbackQuery, state: FSMContext) -> None:
    await state.clear()
    if not call.from_user:
        await ack_callback(call)
        return
    msg = _callback_edit_target(call)
    if not msg:
        await ack_callback(call, text="Откройте меню: /start", show_alert=True)
        return
    await ack_callback(call)
    async with SessionLocal() as session:
        u = await get_or_create_user(
            session,
            tg_id=call.from_user.id,
            username=call.from_user.username,
        )
        promo_s = await _profile_discount_promo_sentence(session, u)
        await session.commit()
    await nav_edit(msg, _profile_text(u, call.from_user.username, promo_sentence=promo_s), profile_kb())


@router.callback_query(F.data == "profile_balance")
async def profile_balance_legacy(call: CallbackQuery, state: FSMContext) -> None:
    """Старые сообщения могли содержать кнопку «Мой баланс» — показываем актуальный профиль."""
    await state.clear()
    if not call.from_user:
        await ack_callback(call)
        return
    msg = _callback_edit_target(call)
    if not msg:
        await ack_callback(call, text="Откройте меню: /start", show_alert=True)
        return
    await ack_callback(call)
    async with SessionLocal() as session:
        u = await get_or_create_user(
            session,
            tg_id=call.from_user.id,
            username=call.from_user.username,
        )
        promo_s = await _profile_discount_promo_sentence(session, u)
        await session.commit()
    await nav_edit(msg, _profile_text(u, call.from_user.username, promo_sentence=promo_s), profile_kb())


@router.callback_query(F.data == "profile_topup")
async def profile_topup(call: CallbackQuery, state: FSMContext) -> None:
    if not call.from_user:
        await ack_callback(call)
        return
    msg = _callback_edit_target(call)
    if not msg:
        await ack_callback(call, text="Откройте меню: /start", show_alert=True)
        return
    await state.set_state(TopupStates.waiting_amount)
    await ack_callback(call)
    mn = _effective_min_topup_rub()
    await nav_edit(
        msg,
        f"💳 <b>Пополнение баланса</b>\n"
        f"Отправьте число — сумма в <b>рублях</b> (от {mn} до {MAX_TOPUP_RUB}).",
        topup_cancel_kb(),
    )


@router.callback_query(F.data == "topup_cancel")
async def topup_cancel_cb(call: CallbackQuery, state: FSMContext) -> None:
    await state.clear()
    msg = _callback_edit_target(call)
    if not msg:
        await ack_callback(call, text="Пополнение отменено.")
        return
    await ack_callback(call)
    tg_uid = call.from_user.id if call.from_user else None
    await nav_edit(msg, START_WELCOME_HTML, _welcome_main_kb(tg_uid))


@router.message(Command("cancel"), StateFilter(TopupStates.waiting_amount))
async def topup_cancel(message: Message, state: FSMContext) -> None:
    await state.clear()
    if message.from_user:
        await reply_welcome_from_user_msg(message)
    else:
        await message.answer(START_WELCOME_HTML + "\n\n<i>Пополнение отменено.</i>")


async def _execute_topup_payment(message: Message, rub: int) -> None:
    """Создать платёж пополнения (ЮKassa или Telegram invoice) после валидации суммы."""
    if not message.from_user:
        return
    minor = rub * 100
    payload = format_topup_payload(message.from_user.id, minor)

    if settings.use_yookassa:
        ret = (settings.yookassa_return_url or "").strip()
        if not ret:
            await message.answer(
                "ЮKassa включена, но не задан <code>YOOKASSA_RETURN_URL</code> в .env.",
                parse_mode="HTML",
            )
            return
        amount_rub = f"{rub:.2f}"
        try:
            yk_data = await create_redirect_payment(
                shop_id=settings.yookassa_shop_id.strip(),
                secret_key=settings.yookassa_secret_key.strip(),
                amount_value_rub=amount_rub,
                return_url=ret,
                description=f"Пополнение баланса OrientalVPN {rub} ₽"[:128],
                metadata={
                    "tg_id": str(message.from_user.id),
                    "type": "topup",
                    "amount_minor": str(minor),
                },
            )
        except YookassaError:
            log.exception("yookassa_topup_failed", extra={"tg_id": message.from_user.id})
            await message.answer("Не удалось создать платёж ЮKassa.")
            return

        pay_id = yk_data.get("id") or ""
        conf = yk_data.get("confirmation") or {}
        pay_url = conf.get("confirmation_url") or ""
        if not pay_id or not pay_url:
            await message.answer("ЮKassa вернула неполный ответ.")
            return

        async with SessionLocal() as session:
            db_user = await get_or_create_user(
                session,
                tg_id=message.from_user.id,
                username=message.from_user.username,
            )
            await create_payment(
                session=session,
                user_id=db_user.id,
                tg_charge_id=f"yookassa:{pay_id}",
                provider_charge_id=pay_id,
                amount_minor=minor,
                currency="RUB",
                invoice_payload=payload,
                status="pending_yookassa",
            )
            await session.commit()

        kb = InlineKeyboardMarkup(
            inline_keyboard=[
                [InlineKeyboardButton(text="💳 Оплатить (ЮKassa)", url=pay_url)],
                [InlineKeyboardButton(text="🔄 Проверить оплату", callback_data=f"yk:{pay_id}")],
                [InlineKeyboardButton(text="❌ Отменить", callback_data="menu_home")],
            ]
        )
        await message.answer(
            f"Счёт на пополнение: <code>{amount_rub} ₽</code>.\n"
            "Оплатите и нажмите «Проверить оплату».",
            reply_markup=kb,
            parse_mode="HTML",
        )
        return

    if not (settings.provider_token or "").strip():
        await message.answer(
            "Оплата не настроена: укажите <code>YOOKASSA_*</code> и при необходимости "
            "<code>PAYMENT_PROVIDER=yookassa</code>, либо <code>PROVIDER_TOKEN</code> для Telegram Payments.",
            parse_mode="HTML",
        )
        return

    min_rub = _effective_min_topup_rub()
    if rub < min_rub:
        await message.answer(
            f"Сумма должна быть от {min_rub} до {MAX_TOPUP_RUB} ₽. Введите другое число.",
        )
        return

    try:
        await message.bot.send_invoice(
            chat_id=message.chat.id,
            title=f"Баланс OrientalVPN · {rub} ₽",
            description="Пополнение внутреннего баланса для оплаты подписки",
            payload=payload,
            provider_token=settings.provider_token,
            currency="RUB",
            prices=[LabeledPrice(label=f"Пополнение {rub} ₽", amount=minor)],
            reply_markup=payment_cancel_to_main_kb(),
        )
    except TelegramBadRequest as e:
        log.exception("topup_send_invoice_failed", extra={"tg_id": message.from_user.id, "error": str(e)})
        await message.answer(_user_text_for_invoice_bad_request(e))


def _looks_like_topup_prompt(msg: Message) -> bool:
    t = msg.text or msg.caption or ""
    return isinstance(t, str) and "Пополнение баланса" in t


@router.message(TopupStates.waiting_amount)
async def topup_amount_entered(message: Message, state: FSMContext) -> None:
    if not message.from_user:
        return
    if getattr(message, "successful_payment", None) is not None:
        raise SkipHandler()
    if message.content_type != ContentType.TEXT:
        await message.answer("Пришлите сумму текстом одним числом в рублях (например: 1500).")
        return
    raw = (message.text or "").strip()
    if not raw:
        return
    if raw.startswith("/"):
        return
    if not raw.isdigit():
        await message.answer("Нужно целое число рублей, например: 500")
        return
    rub = int(raw)
    mn = _effective_min_topup_rub()
    if rub < mn or rub > MAX_TOPUP_RUB:
        await message.answer(f"Допустимо от {mn} до {MAX_TOPUP_RUB} ₽.")
        return
    await state.clear()
    await _execute_topup_payment(message, rub)


@router.message(
    ~StateFilter(TopupStates.waiting_amount),
    F.text.regexp(r"^[1-9]\d{0,8}$"),
    F.reply_to_message,
)
async def topup_amount_reply_fallback(message: Message, state: FSMContext) -> None:
    """
    Рестарт топап после ответом на промпт пополнения, если состояние FSM потерялось (после redeploy без Redis).
    """
    rep = message.reply_to_message
    if rep is None or not rep.from_user or not rep.from_user.is_bot or not _looks_like_topup_prompt(rep):
        return
    if not message.from_user:
        return
    raw = (message.text or "").strip()
    rub = int(raw)
    mn = _effective_min_topup_rub()
    if rub < mn or rub > MAX_TOPUP_RUB:
        await message.answer(f"Допустимо от {mn} до {MAX_TOPUP_RUB} ₽.")
        return
    await state.clear()
    await _execute_topup_payment(message, rub)


@router.callback_query(F.data == "profile_promo")
async def profile_promo_open(call: CallbackQuery, state: FSMContext) -> None:
    if not call.from_user:
        await ack_callback(call)
        return
    msg = _callback_edit_target(call)
    if not msg:
        await ack_callback(call, text="Откройте меню: /start", show_alert=True)
        return
    await ack_callback(call)
    await state.set_state(PromoStates.waiting_code)
    await nav_edit(
        msg,
        "🎟️ <b>Промокод</b>\nОтправьте код одним сообщением.",
        promo_cancel_kb(),
    )


@router.callback_query(F.data == "promo_cancel")
async def promo_cancel_cb(call: CallbackQuery, state: FSMContext) -> None:
    await state.clear()
    if not call.from_user:
        await ack_callback(call)
        return
    await ack_callback(call)
    msg = _callback_edit_target(call)
    async with SessionLocal() as session:
        u = await get_or_create_user(
            session,
            tg_id=call.from_user.id,
            username=call.from_user.username,
        )
        promo_s = await _profile_discount_promo_sentence(session, u)
        await session.commit()
    if msg:
        await nav_edit(msg, _profile_text(u, call.from_user.username, promo_sentence=promo_s), profile_kb())
    elif call.message:
        await call.message.answer(
            _profile_text(u, call.from_user.username, promo_sentence=promo_s),
            parse_mode="HTML",
            reply_markup=profile_kb(),
        )


@router.callback_query(F.data == "profile_referrals")
async def profile_referrals(call: CallbackQuery, state: FSMContext) -> None:
    await state.clear()
    if not call.from_user:
        await ack_callback(call)
        return
    msg = _callback_edit_target(call)
    if not msg:
        await ack_callback(call, text="Откройте меню: /start", show_alert=True)
        return
    await ack_callback(call)
    tg = call.from_user.id
    link = referral_deeplink(tg)
    async with SessionLocal() as session:
        u = await get_or_create_user(session, tg, call.from_user.username)
        regs = await count_referrals_registered(session, u.id)
        paid = await count_referrals_with_tariff_payment(session, u.id)
        await session.commit()
    ref_bonus_pct = settings.referral_commission_bps / 100.0
    text = (
        "🤝 <b>Пригласите друга — оба экономят время</b>\n\n"
        "Отправляйте ссылку ниже: если человек <b>впервые</b> нажмёт /start через неё, "
        "будет учтено приглашение. После оплаты им тарифа вам на баланс начисляется "
        f"<b>{ref_bonus_pct:.0f}%</b> от суммы его платежа (после скидок).\n\n"
        "<b>Ваша персональная ссылка</b>\n"
        f"{html_escape.escape(link)}\n\n"
        "<b>По счёту приглашений в боте:</b>\n"
        f"• перешло по вашей ссылке и сохранено в базе как приглашённые: <b>{regs}</b>\n"
        f"• из них оплатили тариф хотя бы раз: <b>{paid}</b>\n\n"
        "<i>Учитываются только люди с новым /start через ссылку, не уже существовавшие в боте.</i>"
    )
    await nav_edit(msg, text, profile_only_back_kb())


@router.message(StateFilter(PromoStates.waiting_code), F.text)
async def promo_code_enter(message: Message, state: FSMContext) -> None:
    if not message.from_user:
        return
    raw = (message.text or "").strip()
    if raw.startswith("/"):
        return
    code = normalize_promo_code(raw)
    if not code:
        await message.answer("Код не распознан. Пример: SUMMER2026")
        return

    async with SessionLocal() as session:
        user = await get_or_create_user(
            session,
            tg_id=message.from_user.id,
            username=message.from_user.username,
        )
        pr = await get_promo_by_code(session, code)
        if not pr:
            await message.answer("Такого промокода нет или он отключён.")
            return
        err = await validate_promo_for_apply(session, pr, user.id)
        if err:
            await message.answer(err)
            return

        if pr.kind == "free_days":
            days = int(pr.bonus_days or 0)
            if days <= 0:
                await message.answer("У промокода задан некорректный срок. Обратитесь в поддержку.")
                return
            if not available_location_codes():
                await message.answer(NO_VPN_NODES_TEXT)
                return
            chosen = pick_primary_node()
            if not chosen:
                await message.answer("Нет доступных серверов в конфигурации.")
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
                        message.from_user.id,
                        loc_code,
                        node=chosen,
                        days=days,
                    ),
                    retries=settings.provision_retries,
                    base_delay_seconds=1.0,
                )
            except Exception:
                await session.rollback()
                log.exception("promo_free_days_failed", extra={"tg_id": message.from_user.id, "code": code})
                await message.answer("Не удалось выдать доступ по промокоду. Попробуйте позже.")
                return
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
            await record_free_days_promo_redemption(session, pr.id, user.id)
            await session.commit()
            await state.clear()
            await message.answer(
                f"✅ Промокод применён: <b>{days}</b> дн. доступа ко всем серверам.\n"
                f"{subscription_url_pre_block(sub_row.subscription_url)}\n"
                "Откройте «Мои подписки» для ссылки позже.",
                parse_mode="HTML",
                disable_web_page_preview=True,
                reply_markup=main_menu_kb(is_admin=_is_admin_user(message.from_user.id)),
            )
            return

        if pr.kind in ("percent", "fixed"):
            await set_user_active_promo(session, user.id, pr.id)
            await session.commit()
            await state.clear()
            pct = ""
            if pr.kind == "percent":
                pct = f"Скидка <b>{pr.discount_percent}%</b> на цену тарифа при оплате в «Купить».\n\n"
            else:
                fm = pr.discount_fixed_minor or 0
                pct = f"Фикс. скидка <code>{fm / 100:.2f} ₽</code> от цены тарифа при оплате.\n\n"
            await message.answer(
                "🎟️ <b>Промокод сохранён</b>\n"
                f"<code>{html_escape.escape(pr.code)}</code>\n"
                f"{pct}"
                "Следующее оформление подписки учтёт скидку автоматически.",
                parse_mode="HTML",
                reply_markup=main_menu_kb(is_admin=_is_admin_user(message.from_user.id)),
            )
            return

        await message.answer("Тип промокода не поддерживается в боте.")


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
    lines = await _build_subscription_lines(subs)
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
    plan_code = parts[1] if len(parts) > 1 else ""
    if not available_location_codes():
        await ack_callback(call)
        await nav_edit(
            msg,
            NO_VPN_NODES_TEXT,
            main_menu_kb(is_admin=_is_admin_user(call.from_user.id if call.from_user else None)),
            parse_mode=None,
        )
        return
    plan = PLAN_MAP.get(plan_code)
    if not plan:
        await ack_callback(call, text="Тариф не найден", show_alert=True)
        return

    await ack_callback(call)

    plan_amount = int(plan["amount"])
    discounted = plan_amount
    promo_invoice_id: int | None = None
    promo_discount_minor = 0

    async with SessionLocal() as session:
        db_user = await get_or_create_user(
            session,
            tg_id=call.from_user.id,
            username=call.from_user.username,
        )
        if db_user.active_promo_id:
            pr = await get_promo_by_id(session, db_user.active_promo_id)
            if pr and pr.kind in ("percent", "fixed"):
                promo_err = await validate_discount_promo_for_checkout(session, pr, db_user.id)
                if promo_err is None:
                    discounted, promo_discount_minor = promo_discount_on_amount(plan_amount, pr)
                    promo_invoice_id = pr.id
        apply_minor = min(int(db_user.balance_minor), discounted)
        await session.commit()

    charge = discounted - apply_minor
    payload = encode_plan_invoice_payload(
        plan_code=plan_code,
        location_code="all",
        buyer_tg_id=call.from_user.id,
        balance_applied_minor=apply_minor,
        promo_id=promo_invoice_id,
    )

    if charge == 0:
        async with SessionLocal() as session:
            u = await get_or_create_user(
                session,
                tg_id=call.from_user.id,
                username=call.from_user.username,
            )
            if u.balance_minor < discounted:
                await call.message.answer(
                    "Недостаточно баланса. Откройте профиль и проверьте сумму.",
                )
                return
            pay_row = await create_payment(
                session=session,
                user_id=u.id,
                tg_charge_id=f"balance:{uuid4()}",
                provider_charge_id="balance",
                amount_minor=0,
                currency="RUB",
                invoice_payload=payload,
                status="pending_provision",
            )
            await session.flush()
            outcome = await fulfill_paid_payment_row(session, pay_row, buyer_tg_id=call.from_user.id)
            await session.commit()

        if outcome.ok and outcome.subscription_url:
            await call.message.answer(
                "Подписка оплачена с баланса.\n"
                "Доступ: все серверы"
                f"{subscription_url_pre_block(outcome.subscription_url)}\n"
                "Инструкция: клиент → вставить ссылку → обновить профиль.",
                parse_mode="HTML",
                disable_web_page_preview=True,
            )
        elif outcome.already_done:
            await call.message.answer("Доступ уже был выдан. Смотрите «Мои подписки».")
        elif outcome.no_node:
            await call.message.answer(
                "Сейчас нет свободной ноды. Попробуйте позже — зачисление повторится автоматически.",
            )
        elif outcome.marzban_error:
            await call.message.answer(
                "Временно не удалось выдать доступ. Повтор попытки — автоматически.",
            )
        return

    if settings.use_yookassa:
        ret = (settings.yookassa_return_url or "").strip()
        if not ret:
            await call.message.answer(
                "ЮKassa включена, но не задан <code>YOOKASSA_RETURN_URL</code> в .env "
                "(HTTPS, например <code>https://t.me/…</code>).",
                parse_mode="HTML",
            )
            return
        amount_rub = f"{charge / 100:.2f}"
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
                    "balance_applied_minor": str(apply_minor),
                    "promo_id": str(promo_invoice_id) if promo_invoice_id else "",
                    "discounted_minor": str(discounted),
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
                amount_minor=charge,
                currency="RUB",
                invoice_payload=payload,
                status="pending_yookassa",
            )
            await session.commit()

        kb = InlineKeyboardMarkup(
            inline_keyboard=[
                [InlineKeyboardButton(text="💳 Оплатить (ЮKassa)", url=pay_url)],
                [InlineKeyboardButton(text="🔄 Проверить оплату", callback_data=f"yk:{pay_id}")],
                [InlineKeyboardButton(text="❌ Отменить", callback_data="menu_home")],
            ]
        )
        extra = ""
        if apply_minor > 0:
            extra = f"\nУчтено с баланса: <code>{apply_minor / 100:.2f} ₽</code>."
        if promo_discount_minor > 0:
            extra += f"\nСкидка по промокоду: <code>{promo_discount_minor / 100:.2f} ₽</code>."
        await call.message.answer(
            f"💳 Счёт: <b>{html_escape.escape(plan['title'])}</b>\n"
            f"К оплате: <code>{amount_rub} ₽</code>{extra}\n\n"
            "Нажмите «Оплатить», завершите оплату на сайте ЮKassa, затем «Проверить оплату».",
            reply_markup=kb,
            parse_mode="HTML",
            disable_web_page_preview=True,
        )
        return

    if not (settings.provider_token or "").strip():
        await call.message.answer(
            "Оплата не настроена: задайте ключи ЮKassa и при необходимости "
            "<code>PAYMENT_PROVIDER=yookassa</code>, либо <code>PROVIDER_TOKEN</code> Telegram Payments.",
            parse_mode="HTML",
            disable_web_page_preview=True,
        )
        return

    desc = "OrientalVPN — все серверы в одной подписке"
    if apply_minor > 0:
        desc += f" (с баланса −{apply_minor / 100:.2f} ₽)"
    if promo_discount_minor > 0:
        desc += f" (промо −{promo_discount_minor / 100:.2f} ₽)"
    prices = [LabeledPrice(label=plan["title"], amount=charge)]
    try:
        await call.bot.send_invoice(
            chat_id=msg.chat.id,
            title=plan["title"],
            description=desc,
            payload=payload,
            provider_token=settings.provider_token,
            currency="RUB",
            prices=prices,
            reply_markup=payment_cancel_to_main_kb(),
            need_email=False,
            need_name=False,
            need_phone_number=False,
        )
    except TelegramBadRequest as e:
        log.exception("plan_send_invoice_failed", extra={"tg_id": call.from_user.id, "error": str(e)})
        await call.message.answer(
            _user_text_for_invoice_bad_request(e),
            disable_web_page_preview=True,
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
        reply_markup=main_menu_kb(is_admin=_is_admin_user(tg_user.id if tg_user else None)),
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
    await nav_edit(
        msg,
        "⏳ Выдаём пробный доступ…",
        main_menu_kb(is_admin=_is_admin_user(call.from_user.id if call.from_user else None)),
    )
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
                main_menu_kb(is_admin=_is_admin_user(call.from_user.id if call.from_user else None)),
                parse_mode=None,
            )
            return
        subs = await list_active_subscriptions_for_user(session, user.id)
    if not subs:
        await nav_edit(
            msg,
            subscriptions_list_intro_html() + "\n\n<i>Активных подписок нет.</i>",
            main_menu_kb(is_admin=_is_admin_user(call.from_user.id if call.from_user else None)),
        )
        return
    lines = await _build_subscription_lines(subs)
    await nav_edit(
        msg,
        subscriptions_list_intro_html() + "\n\n" + "\n\n".join(lines),
        main_menu_kb(is_admin=_is_admin_user(call.from_user.id if call.from_user else None)),
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
    lines = await _build_subscription_lines(subs)
    await message.answer(
        subscriptions_list_intro_html() + "\n\n" + "\n\n".join(lines),
        parse_mode="HTML",
        disable_web_page_preview=True,
    )
