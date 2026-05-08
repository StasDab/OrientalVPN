import html as html_escape
import logging
import re
from uuid import uuid4

from aiogram import F, Router
from aiogram.exceptions import TelegramBadRequest
from aiogram.filters import Command, StateFilter
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, LabeledPrice, Message

from app.config import settings
from app.datetime_util import naive_utc_from_timestamp, utc_now_naive
from app.db.models import Subscription, User
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
    topup_cancel_kb,
)
from app.payment_fulfillment import fulfill_paid_payment_row
from app.plans import LOCATION_TITLES, PLAN_MAP, format_topup_payload
from app.services.node_registry import (
    available_location_codes,
    pick_primary_node,
)
from app.services.retry import with_retry
from app.services.vpn_provider import MarzbanAdapter
from app.services.yookassa import YookassaError, create_redirect_payment
from app.states import TopupStates
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

HELP_TEXT = (
    "Как подключиться:\n"
    "1) Установите клиент (Happ, v2rayTun, Nekoray и т.п.).\n"
    "2) Добавьте подписку по ссылке из бота.\n"
    "3) Обновите список серверов в клиенте.\n\n"
    "Команды: /buy — тарифы, /trial — пробный доступ, "
    "/my — подписки, /profile — профиль, /help — эта справка.\n\n"
    "«Купить» — оформление подписки: к оплате будет сумма тарифа минус ваш баланс.\n"
    "«Пополнить баланс» в профиле — введите сумму в рублях; пополненный баланс учтётся в счёте подписки.\n\n"
    "Пробный доступ и оплата дают одну ссылку подписки со всеми серверами — она в разделе «Мои подписки»."
)


async def nav_edit(
    message: Message,
    text: str,
    reply_markup: InlineKeyboardMarkup | None = None,
    *,
    parse_mode: str | None = "HTML",
) -> None:
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


def _profile_text(db_user: User, display_username: str | None) -> str:
    uname = f"@{html_escape.escape(display_username)}" if display_username else "—"
    bal = db_user.balance_minor / 100
    return (
        "👤 <b>Профиль</b>\n"
        f"Telegram ID: <code>{db_user.tg_id}</code>\n"
        f"Username: {uname}\n\n"
        f"💰 <b>Баланс:</b> <code>{bal:.2f} ₽</code>\n\n"
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
async def cmd_start(message: Message) -> None:
    text = (
        "👋 <b>Добро пожаловать в OrientalVPN!</b>\n\n"
        "✨ Твой личный ключ к безграничному интернету:\n\n"
        "💎 <b>Высокоскоростной и Надежный VPN:</b>\n"
        "• ⚡️Мгновенная загрузка 4K-видео и стабильный стриминг.\n"
        "• 🛡 Обход Глушилок (DPI): Используем технологию Stealth для надежного доступа в любых условиях.\n"
        "• 🌍 Открытие всех заблокированных ресурсов: Instagram, TikTok, Netflix, Discord.\n"
        "• 🔒 Полная анонимность и защита трафика."
    )
    banner = (settings.start_banner_url or "").strip()
    if banner:
        await message.answer_photo(
            photo=banner,
            caption=text,
            parse_mode="HTML",
            reply_markup=main_menu_kb(),
        )
        return
    await message.answer(text, reply_markup=main_menu_kb(), parse_mode="HTML")


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
async def callback_menu_home(call: CallbackQuery, state: FSMContext) -> None:
    await state.clear()
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
        await session.commit()
    await message.answer(
        _profile_text(u, message.from_user.username),
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
        await session.commit()
    await nav_edit(msg, _profile_text(u, call.from_user.username), profile_kb())


@router.callback_query(F.data == "profile_balance")
async def profile_balance(call: CallbackQuery) -> None:
    if not call.from_user:
        await ack_callback(call)
        return
    msg = _callback_edit_target(call)
    if not msg:
        await ack_callback(call, text="Откройте меню: /start", show_alert=True)
        return
    await ack_callback(call)
    async with SessionLocal() as session:
        u = await get_user_by_tg_id(session, call.from_user.id)
        bal = (u.balance_minor if u else 0) / 100
    await nav_edit(
        msg,
        f"💰 <b>Мой баланс</b>\nСейчас: <code>{bal:.2f} ₽</code>\n\n"
        "Средства спишутся при оплате подписки (или целиком, если баланса хватает на тариф).",
        subscriptions_back_kb(),
    )


@router.callback_query(F.data == "profile_topup")
async def profile_topup(call: CallbackQuery, state: FSMContext) -> None:
    if not call.from_user:
        await ack_callback(call)
        return
    await ack_callback(call)
    await state.set_state(TopupStates.waiting_amount)
    await call.message.answer(
        f"💳 <b>Пополнение баланса</b>\n"
        f"Отправьте число — сумма в <b>рублях</b> (от {MIN_TOPUP_RUB} до {MAX_TOPUP_RUB}).",
        parse_mode="HTML",
        reply_markup=topup_cancel_kb(),
    )


@router.callback_query(StateFilter(TopupStates.waiting_amount), F.data == "topup_cancel")
async def topup_cancel_cb(call: CallbackQuery, state: FSMContext) -> None:
    await state.clear()
    msg = _callback_edit_target(call)
    if not msg:
        await ack_callback(call, text="Пополнение отменено.")
        return
    await ack_callback(call)
    await nav_edit(msg, "🏠 <b>Главное меню</b>\nВыберите раздел:", main_menu_kb())


@router.message(Command("cancel"), StateFilter(TopupStates.waiting_amount))
async def topup_cancel(message: Message, state: FSMContext) -> None:
    await state.clear()
    await message.answer("Пополнение отменено.", reply_markup=main_menu_kb())


@router.message(TopupStates.waiting_amount, F.text)
async def topup_amount_entered(message: Message, state: FSMContext) -> None:
    if not message.from_user:
        return
    raw = (message.text or "").strip()
    if raw.startswith("/"):
        return
    if not raw.isdigit():
        await message.answer("Нужно целое число рублей, например: 500")
        return
    rub = int(raw)
    if rub < MIN_TOPUP_RUB or rub > MAX_TOPUP_RUB:
        await message.answer(f"Допустимо от {MIN_TOPUP_RUB} до {MAX_TOPUP_RUB} ₽.")
        return
    minor = rub * 100
    await state.clear()
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
            "Платежи Telegram не настроены: задайте <code>PROVIDER_TOKEN</code> в .env.",
            parse_mode="HTML",
        )
        return

    await message.bot.send_invoice(
        chat_id=message.chat.id,
        title=f"Баланс OrientalVPN · {rub} ₽",
        description="Пополнение внутреннего баланса для оплаты подписки",
        payload=payload,
        provider_token=settings.provider_token,
        currency="RUB",
        prices=[LabeledPrice(label=f"Пополнение {rub} ₽", amount=minor)],
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
    payment_step_back = parts[2] if len(parts) > 2 and parts[2] in ("buy", "profile") else "buy"
    if not available_location_codes():
        await ack_callback(call)
        await nav_edit(msg, NO_VPN_NODES_TEXT, main_menu_kb(), parse_mode=None)
        return
    plan = PLAN_MAP.get(plan_code)
    if not plan:
        await ack_callback(call, text="Тариф не найден", show_alert=True)
        return

    plan_amount = int(plan["amount"])
    async with SessionLocal() as session:
        db_user = await get_or_create_user(
            session,
            tg_id=call.from_user.id,
            username=call.from_user.username,
        )
        apply_minor = min(int(db_user.balance_minor), plan_amount)
        await session.commit()

    charge = plan_amount - apply_minor
    payload = f"{plan_code}:all:{call.from_user.id}:{apply_minor}"

    await ack_callback(call)
    pay_hint = ""
    if apply_minor > 0:
        pay_hint = (
            f"\n\nС баланса будет учтено <code>{apply_minor / 100:.2f} ₽</code>, "
            f"к оплате <code>{charge / 100:.2f} ₽</code>."
        )
    if charge == 0:
        pay_hint = "\n\nТариф полностью оплачивается с баланса — отдельный счёт не нужен."

    await nav_edit(
        msg,
        "💳 <b>Оплата подписки</b>\nДоступ ко <b>всем серверам</b> в одной ссылке."
        f"{pay_hint}",
        plans_kb(back_to=payment_step_back, payment_step_back=payment_step_back),
    )

    if charge == 0:
        async with SessionLocal() as session:
            u = await get_or_create_user(
                session,
                tg_id=call.from_user.id,
                username=call.from_user.username,
            )
            if u.balance_minor < plan_amount:
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
            ]
        )
        extra = ""
        if apply_minor > 0:
            extra = f"\nУчтено с баланса: <code>{apply_minor / 100:.2f} ₽</code>."
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
            "Платежи Telegram не настроены: в .env задайте <code>PROVIDER_TOKEN</code> "
            "(BotFather → бот → Payments, токен после привязки ЮKassa) и перезапустите бота. "
            "Должно быть <code>PAYMENT_PROVIDER=telegram</code>.",
            parse_mode="HTML",
            disable_web_page_preview=True,
        )
        return

    desc = "OrientalVPN — все серверы в одной подписке"
    if apply_minor > 0:
        desc += f" (с баланса −{apply_minor / 100:.2f} ₽)"
    prices = [LabeledPrice(label=plan["title"], amount=charge)]
    await call.bot.send_invoice(
        chat_id=msg.chat.id,
        title=plan["title"],
        description=desc,
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
    lines = await _build_subscription_lines(subs)
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
    lines = await _build_subscription_lines(subs)
    await message.answer(
        subscriptions_list_intro_html() + "\n\n" + "\n\n".join(lines),
        parse_mode="HTML",
        disable_web_page_preview=True,
    )
