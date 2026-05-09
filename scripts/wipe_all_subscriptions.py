#!/usr/bin/env python3
"""
Удаляет все строки subscriptions в БД бота (PostgreSQL).

Не меняет users.trial_used, не трогает Marzban. Запись subscription_devices удаляется каскадом FK.

На VPS из корня репозитория:
  /opt/myvpn/.venv/bin/python scripts/wipe_all_subscriptions.py --dry-run
  /opt/myvpn/.venv/bin/python scripts/wipe_all_subscriptions.py --apply
"""
from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path


def main() -> int:
    p = argparse.ArgumentParser(description="Удалить все подписки в БД бота (trial не трогать)")
    p.add_argument("--dry-run", action="store_true", help="Только показать COUNT(*)")
    p.add_argument("--apply", action="store_true", help="Выполнить DELETE FROM subscriptions")
    args = p.parse_args()
    if not args.apply and not args.dry_run:
        print("Укажите --dry-run и/или --apply", file=sys.stderr)
        return 1

    try:
        from dotenv import load_dotenv
    except ImportError:
        load_dotenv = None  # type: ignore[assignment]
    env_path = Path(__file__).resolve().parents[1] / ".env"
    app_env = Path(__file__).resolve().parents[1] / "app" / ".env"
    if load_dotenv:
        load_dotenv(env_path, override=True)
        if app_env.is_file():
            load_dotenv(app_env, override=True)

    raw_url = os.getenv("DATABASE_URL", "").strip()
    if not raw_url:
        print(
            "Нет DATABASE_URL. Задайте в .env корня (/opt/myvpn) или app/.env",
            file=sys.stderr,
        )
        return 1
    dsn = raw_url.replace("postgresql+asyncpg", "postgresql", 1)
    dsn = dsn.replace("postgresql+psycopg2", "postgresql", 1)

    import psycopg2

    try:
        conn = psycopg2.connect(dsn)
    except psycopg2.OperationalError as e:
        print(f"PostgreSQL: {e}", file=sys.stderr)
        return 1

    conn.autocommit = True
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT COUNT(*) FROM subscriptions")
            n = cur.fetchone()[0]
        print(f"Строк subscriptions: {n}")
        if args.dry_run and not args.apply:
            print("Dry-run: изменений нет.")
            return 0
        if args.apply:
            with conn.cursor() as cur:
                cur.execute("DELETE FROM subscriptions")
                print(f"Удалено строк: {cur.rowcount}")
        print(
            "Готово (Marzban и trial_used не затронуты). "
            "Пользователям нужна новая выдача подписки из бота / ручное создание.",
        )
    finally:
        conn.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
