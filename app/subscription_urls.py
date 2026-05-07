"""Публичная ссылка подписки: опционально через шлюз /sub/{token} с лимитом «устройств»."""

from __future__ import annotations

import uuid

from app.config import settings


def subscription_gate_enabled() -> bool:
    return bool((settings.subscription_gate_public_base or "").strip())


def build_subscription_urls(
    marzban_subscription_url: str,
    *,
    existing_gate_token: str | None,
) -> tuple[str, str | None, str | None, int]:
    """
    Возвращает:
      - url для показа пользователю (публичный шлюз или прямой Marzban);
      - upstream (реальный URL Marzban) — только если шлюз включён, иначе None;
      - токен шлюза — только если шлюз включён;
      - max_devices (из настроек).
    """
    max_dev = int(settings.subscription_max_devices)
    if not subscription_gate_enabled():
        return marzban_subscription_url, None, None, max_dev
    token = (existing_gate_token or "").strip() or str(uuid.uuid4())
    base = (settings.subscription_gate_public_base or "").strip().rstrip("/")
    public = f"{base}/sub/{token}"
    return public, marzban_subscription_url.strip(), token, max_dev
