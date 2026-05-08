from __future__ import annotations

import html


def code_inline(text: str) -> str:
    """HTML-обёртка для копируемого (и не кликабельного) текста в Telegram."""
    return f"<code>{html.escape((text or '').strip())}</code>"


def subscription_url_pre_block(url: str) -> str:
    """
    Блок ссылки для мобильного Telegram: отступ, заголовок и <pre> — удобно тапнуть и скопировать целиком.
    """
    u = html.escape((url or "").strip())
    return (
        "\n\n"
        "▫️ <b>Ссылка подписки</b>\n"
        "<i>На телефоне нажмите на рамку ниже — скопируется вся строка.</i>\n\n"
        f"<pre>{u}</pre>"
    )


def subscription_url_pre_only(url: str) -> str:
    """Только <pre> с экранированием (для списка подписок)."""
    u = html.escape((url or "").strip())
    return f"<pre>{u}</pre>"


def tariff_block(icon: str, title: str, body: str, foot: str | None = None) -> str:
    """Визитка тарифа: заголовок + blockquote Telegram HTML."""
    t = html.escape(title.strip())
    b = html.escape(body.strip())
    inner = f"❌ {icon} <b>{t}</b>\n{b}"
    if foot:
        inner += f"\n<i>{html.escape(foot.strip())}</i>"
    return f"<blockquote expandable>{inner}</blockquote>"


def tariffs_select_html(balance_rub: str = "0 ₽") -> str:
    bal = html.escape((balance_rub or "0 ₽").strip())
    blocks = [
        tariff_block("🏎", "1 месяц:", "Идеально для краткосрочного использования.", "— оптимальный баланс цены и длительности"),
        tariff_block("⚡", "3 месяца:", "Отличный вариант на сезон.", "— оптимальный баланс · выгоднее на ~11%/день vs 30 д"),
        tariff_block("🚀", "6 месяцев:", "Половина года стабильной приватности.", "— оптимальный баланс · выгоднее на ~19%/день vs 30 д"),
        tariff_block("🌍", "12 месяцев:", "Максимальная защита на год.", "— оптимальный баланс · выгоднее на ~30%/день vs 30 д"),
    ]
    return (
        f"💰 <b>Ваш баланс:</b> <code>{bal}</code>\n"
        "✅ <b>Выберите тариф (₽):</b>\n\n"
        + "\n".join(blocks)
    )


def jammer_bypass_help_html() -> str:
    return (
        "🛡️ <b>Обход глушилок — подробная информация</b>\n\n"
        "<b>Что это такое?</b>\n"
        "Обход глушилок — это технология стабильного VPN там, где мобильный интернет "
        "могут ограничивать операторские фильтры, «глушилки» или узкое LTE.\n\n"
        "<b>💡 Как это работает</b>\n"
        "<blockquote expandable>"
        "• Поддержка современных протоколов обфускации (REALITY и др.)\n"
        "• Подключение через доступные узлы без ручной возни\n"
        "• Шифрование трафика и защита от подмен на стороне провайдера"
        "</blockquote>\n\n"
        "<b>📱 Когда это полезно</b>\n"
        "<blockquote expandable>"
        "• В метро и подземных переходах\n"
        "• В местах со слабым сигналом\n"
        "• При частых ограничениях оператора\n"
        "• В регионах с усиленными блокировками"
        "</blockquote>\n\n"
        "<b>⚙️ Технические детали</b>\n"
        "В подписке могут быть несколько узлов разных регионов "
        "(например Европа и США), чтобы можно было быстро поменять маршрут в клиенте."
    )


def subscriptions_list_intro_html() -> str:
    return (
        "📄 <b>Ваши активные подписки</b>\n"
        "Выберите подписку для подробной информации:\n\n"
        "<i>(Ниже — список: сервер и срок окончания; ссылку удобно скопировать тапом по рамке.)</i>"
    )
