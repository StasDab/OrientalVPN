from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

from app.plans import LOCATION_CODES, LOCATION_TITLES, PLAN_MAP


def main_menu_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="Купить", callback_data="buy")],
            [InlineKeyboardButton(text="Пробный доступ", callback_data="trial")],
            [InlineKeyboardButton(text="Мои подписки", callback_data="my")],
            [InlineKeyboardButton(text="Помощь", callback_data="help")],
        ]
    )


def plans_kb() -> InlineKeyboardMarkup:
    labels = {
        "1m": "1 месяц — 490 RUB",
        "3m": "3 месяца — 1200 RUB",
        "12m": "12 месяцев — 4000 RUB",
    }
    rows = [
        [InlineKeyboardButton(text=labels[code], callback_data=f"plan:{code}")]
        for code in ("1m", "3m", "12m")
        if code in PLAN_MAP
    ]
    return InlineKeyboardMarkup(inline_keyboard=rows)


def locations_kb(plan_code: str) -> InlineKeyboardMarkup:
    buttons = []
    for loc in LOCATION_CODES:
        title = LOCATION_TITLES.get(loc, loc.upper())
        buttons.append(
            [InlineKeyboardButton(text=title, callback_data=f"loc:{plan_code}:{loc}")]
        )
    return InlineKeyboardMarkup(inline_keyboard=buttons)


def trial_locations_kb() -> InlineKeyboardMarkup:
    buttons = []
    for loc in LOCATION_CODES:
        title = LOCATION_TITLES.get(loc, loc.upper())
        buttons.append([InlineKeyboardButton(text=title, callback_data=f"trial_loc:{loc}")])
    return InlineKeyboardMarkup(inline_keyboard=buttons)
