#!/usr/bin/env python3
"""
Массовая замена префикса в subscriptions.subscription_url.

Пример:
  /opt/myvpn/.venv/bin/python scripts/replace_subscription_prefix.py \
    --old-prefix https://panel.orientalvpn.ru \
    --new-prefix https://panel.orientalvpn.ru:8443 \
    --apply
"""
from __future__ import annotations

import argparse
import asyncio

from sqlalchemy import select, update

from app.db.models import Subscription
from app.db.session import SessionLocal


def _parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Replace subscription URL prefix in DB")
    p.add_argument("--old-prefix", required=True, help="Current prefix in DB URLs")
    p.add_argument("--new-prefix", required=True, help="Target prefix to set")
    p.add_argument(
        "--apply",
        action="store_true",
        help="Apply changes (without this flag script only shows count)",
    )
    return p.parse_args()


def _normalize_prefix(value: str) -> str:
    return value.strip().rstrip("/")


async def _main() -> int:
    args = _parse_args()
    old_prefix = _normalize_prefix(args.old_prefix)
    new_prefix = _normalize_prefix(args.new_prefix)
    old_like = f"{old_prefix}/%"

    async with SessionLocal() as session:
        rows = await session.execute(
            select(Subscription.id, Subscription.subscription_url).where(
                Subscription.subscription_url.like(old_like)
            )
        )
        matches = rows.all()
        count = len(matches)
        print(f"Найдено ссылок для замены: {count}")
        if not count:
            return 0

        if not args.apply:
            print("Dry-run режим: ничего не изменено. Добавьте --apply для применения.")
            print("Пример первой ссылки:")
            print(matches[0][1])
            return 0

        for sub_id, current_url in matches:
            new_url = f"{new_prefix}{current_url[len(old_prefix):]}"
            await session.execute(
                update(Subscription)
                .where(Subscription.id == sub_id)
                .values(subscription_url=new_url)
            )
        await session.commit()
        print(f"Готово. Обновлено ссылок: {count}")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(_main()))
