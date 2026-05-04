#!/usr/bin/env python3
"""
Сброс «пробный уже использован» и подписок в БД бота (PostgreSQL), не в Marzban.

Удаление пользователей в панели Marzban на флаг trial_used в боте не влияет.

Использование на VPS:
  cd /opt/myvpn/app
  /opt/myvpn/.venv/bin/python scripts/reset_trial_for_tg.py 731162352

Переменная DATABASE_URL берётся из окружения или из .env в корне репозитория.
"""
from __future__ import annotations

import os
import sys
from pathlib import Path


def main() -> int:
    if len(sys.argv) < 2:
        print("Укажите Telegram user id: python scripts/reset_trial_for_tg.py <tg_id>", file=sys.stderr)
        return 1
    try:
        tg_id = int(sys.argv[1].strip())
    except ValueError:
        print("tg_id должен быть числом", file=sys.stderr)
        return 1

    try:
        from dotenv import load_dotenv
    except ImportError:
        load_dotenv = None  # type: ignore[assignment]
    if load_dotenv:
        load_dotenv(Path(__file__).resolve().parents[1] / ".env", override=False)

    raw_url = os.getenv("DATABASE_URL", "").strip()
    if not raw_url:
        print("Нет DATABASE_URL", file=sys.stderr)
        return 1
    # psycopg2.connect принимает libpq URI только как postgresql://..., не postgresql+psycopg2://
    dsn = raw_url.replace("postgresql+asyncpg", "postgresql", 1)
    dsn = dsn.replace("postgresql+psycopg2", "postgresql", 1)

    import psycopg2

    conn = psycopg2.connect(dsn)
    conn.autocommit = True
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT id FROM users WHERE tg_id = %s", (tg_id,))
            row = cur.fetchone()
            if not row:
                print(f"Пользователь tg_id={tg_id} не найден в users — создастся при /start.")
                return 0
            uid = row[0]
            cur.execute("DELETE FROM subscriptions WHERE user_id = %s", (uid,))
            cur.execute("UPDATE users SET trial_used = false WHERE id = %s", (uid,))
        print(f"Готово: tg_id={tg_id}, user_id={uid}: trial_used=false, подписки удалены из бота.")
        print("Пользователей в Marzban при необходимости удалите вручную в панели.")
    finally:
        conn.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
