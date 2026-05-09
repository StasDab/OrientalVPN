#!/usr/bin/env python3
"""
Удаляет все строки subscriptions одного пользователя в БД бота по Telegram user id.

Не меняет trial_used, не трогает Marzban напрямую.
Чтобы также отключить доступ в панели, после этого выполните в боте: /wipe_subs <tg_id>

На VPS из корня репозитория:
  /opt/myvpn/.venv/bin/python scripts/wipe_subscriptions_for_tg.py 731162352
"""
from __future__ import annotations

import os
import sys
from pathlib import Path


def main() -> int:
    if len(sys.argv) < 2:
        print("Укажите Telegram user id:", file=sys.stderr)
        print("  python scripts/wipe_subscriptions_for_tg.py <tg_id>", file=sys.stderr)
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
    env_path = Path(__file__).resolve().parents[1] / ".env"
    app_env = Path(__file__).resolve().parents[1] / "app" / ".env"
    if load_dotenv:
        load_dotenv(env_path, override=True)
        if app_env.is_file():
            load_dotenv(app_env, override=True)

    raw_url = os.getenv("DATABASE_URL", "").strip()
    if not raw_url:
        print("Нет DATABASE_URL", file=sys.stderr)
        return 1
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
                print(f"tg_id={tg_id} не найден в users (пользователь может ещё не нажать /start).")
                return 0
            uid = row[0]
            cur.execute(
                "SELECT COUNT(*) FROM subscriptions WHERE user_id = %s",
                (uid,),
            )
            cnt = cur.fetchone()[0]
            cur.execute("DELETE FROM subscriptions WHERE user_id = %s", (uid,))
            deleted = cur.rowcount
        print(f"tg_id={tg_id}, user_id={uid}: удалено записей subscriptions: {deleted} (до удаления было {cnt}).")
        print("Marzban не изменён — при необходимости: /wipe_subs или ручное отключение в панели.")
    finally:
        conn.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
