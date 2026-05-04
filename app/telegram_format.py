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
