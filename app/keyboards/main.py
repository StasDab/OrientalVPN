from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

from app.plans import LOCATION_TITLES, PLAN_MAP
from app.services.node_registry import available_location_codes


def main_menu_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="👤 Профиль", callback_data="profile")],
            [InlineKeyboardButton(text="💳 Купить", callback_data="buy")],
            [InlineKeyboardButton(text="Пробный доступ", callback_data="trial")],
            [InlineKeyboardButton(text="📁 Мои подписки", callback_data="my")],
            [InlineKeyboardButton(text="Помощь", callback_data="help")],
        ]
    )


def profile_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="📁 Мои подписки", callback_data="profile_subs")],
            [InlineKeyboardButton(text="🏆 Мой уровень", callback_data="profile_level")],
            [InlineKeyboardButton(text="💰 Мой баланс", callback_data="profile_balance")],
            [InlineKeyboardButton(text="💳 Пополнить баланс", callback_data="profile_topup")],
            [InlineKeyboardButton(text="🎟️ Активировать промокод", callback_data="profile_promo")],
            [InlineKeyboardButton(text="🛡️ Обход глушилок", callback_data="jammer_help")],
            [InlineKeyboardButton(text="🔙 В главное меню", callback_data="menu_home")],
        ]
    )


def plans_kb() -> InlineKeyboardMarkup:
    labels = {
        "1m": "1 месяц — 199 ₽",
        "3m": "3 месяца — 530 ₽",
        "6m": "6 месяцев — 970 ₽",
        "12m": "1 год — 1700 ₽",
    }
    order = ("1m", "3m", "6m", "12m")
    rows = [
        [InlineKeyboardButton(text=labels[code], callback_data=f"plan:{code}")]
        for code in order
        if code in PLAN_MAP
    ]
    return InlineKeyboardMarkup(inline_keyboard=rows)


def subscriptions_back_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="🔙 Назад", callback_data="profile")],
        ]
    )


def locations_kb(plan_code: str) -> InlineKeyboardMarkup:
    buttons = []
    for loc in available_location_codes():
        title = LOCATION_TITLES.get(loc, loc.upper())
        buttons.append(
            [InlineKeyboardButton(text=title, callback_data=f"loc:{plan_code}:{loc}")]
        )
    return InlineKeyboardMarkup(inline_keyboard=buttons)


def trial_locations_kb() -> InlineKeyboardMarkup:
    buttons = []
    for loc in available_location_codes():
        title = LOCATION_TITLES.get(loc, loc.upper())
        buttons.append([InlineKeyboardButton(text=title, callback_data=f"trial_loc:{loc}")])
    return InlineKeyboardMarkup(inline_keyboard=buttons)
