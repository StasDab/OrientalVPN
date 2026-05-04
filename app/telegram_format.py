from __future__ import annotations

import html


def code_inline(text: str) -> str:
    """HTML-обёртка для копируемого (и не кликабельного) текста в Telegram."""
    return f"<code>{html.escape((text or '').strip())}</code>"

