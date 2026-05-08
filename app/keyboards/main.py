from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

from app.plans import PLAN_MAP


def main_menu_kb(*, is_admin: bool = False) -> InlineKeyboardMarkup:
    rows = [
        [InlineKeyboardButton(text="👤 Профиль", callback_data="profile")],
        [InlineKeyboardButton(text="💳 Купить", callback_data="buy")],
        [InlineKeyboardButton(text="Пробный доступ", callback_data="trial")],
        [InlineKeyboardButton(text="📁 Мои подписки", callback_data="my")],
        [InlineKeyboardButton(text="Помощь", callback_data="help")],
    ]
    if is_admin:
        rows.insert(1, [InlineKeyboardButton(text="🛠️ Админ-панель", callback_data="admin_panel")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def profile_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="📁 Мои подписки", callback_data="profile_subs")],
            [InlineKeyboardButton(text="💰 Мой баланс", callback_data="profile_balance")],
            [InlineKeyboardButton(text="💳 Пополнить баланс", callback_data="profile_topup")],
            [InlineKeyboardButton(text="🎟️ Промокод", callback_data="profile_promo")],
            [InlineKeyboardButton(text="🛡️ Обход глушилок", callback_data="jammer_help")],
            [InlineKeyboardButton(text="🔙 В главное меню", callback_data="menu_home")],
        ]
    )


def promo_cancel_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="Отменить", callback_data="promo_cancel")],
        ]
    )


def plans_kb(
    *,
    back_to: str = "menu_home",
    payment_step_back: str = "buy",
) -> InlineKeyboardMarkup:
    """
    back_to — куда ведёт «Назад» на экране списка тарифов.
    payment_step_back — откуда пришли к оплате (buy | profile): тот же токен в callback plan:*,
      чтобы после выбора тарифа «Назад» возвращал к списку тарифов или в профиль.
    """
    labels = {
        "1m": "1 месяц — 199 ₽",
        "3m": "3 месяца — 530 ₽",
        "6m": "6 месяцев — 970 ₽",
        "12m": "1 год — 1700 ₽",
    }
    order = ("1m", "3m", "6m", "12m")
    rows = [
        [InlineKeyboardButton(text=labels[code], callback_data=f"plan:{code}:{payment_step_back}")]
        for code in order
        if code in PLAN_MAP
    ]
    rows.append([InlineKeyboardButton(text="🔙 Назад", callback_data=back_to)])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def subscriptions_back_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="🔙 Назад", callback_data="profile")],
        ]
    )


def topup_cancel_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="❌ Отменить", callback_data="topup_cancel")],
        ]
    )


def admin_panel_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="📊 Статистика", callback_data="admin_stats")],
            [InlineKeyboardButton(text="🖥 Серверы", callback_data="admin_servers")],
            [InlineKeyboardButton(text="🎟 Промокоды", callback_data="admin_promos")],
            [InlineKeyboardButton(text="🤝 Рефералы", callback_data="admin_referrals")],
            [InlineKeyboardButton(text="📣 Рассылка", callback_data="admin_broadcast")],
            [InlineKeyboardButton(text="🔙 В главное меню", callback_data="menu_home")],
        ]
    )


def admin_nav_back_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="🔙 В панель", callback_data="admin_home")],
        ]
    )

