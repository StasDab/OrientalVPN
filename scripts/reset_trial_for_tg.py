#!/usr/bin/env python3
"""
Сброс «пробный уже использован» и подписок в БД бота (PostgreSQL), не в Marzban.

Удаление пользователей в панели Marzban на флаг trial_used в боте не влияет.

Переменная DATABASE_URL: из окружения или из `.env` / `app/.env` в корне репозитория.

Использование на VPS (из корня репозитория `/opt/myvpn`):
  /opt/myvpn/.venv/bin/python scripts/reset_trial_for_tg.py 731162352
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
    env_path = Path(__file__).resolve().parents[1] / ".env"
    app_env = Path(__file__).resolve().parents[1] / "app" / ".env"
    if load_dotenv:
        load_dotenv(env_path, override=True)
        if app_env.is_file():
            load_dotenv(app_env, override=True)

    raw_url = os.getenv("DATABASE_URL", "").strip()
    if not raw_url:
        print(
            "Нет DATABASE_URL. Задайте в /opt/myvpn/app/.env или /opt/myvpn/.env и снова запустите:\n"
            f"  (искали: {env_path}, {app_env})",
            file=sys.stderr,
        )
        return 1
    # psycopg2.connect принимает libpq URI только как postgresql://..., не postgresql+psycopg2://
    dsn = raw_url.replace("postgresql+asyncpg", "postgresql", 1)
    dsn = dsn.replace("postgresql+psycopg2", "postgresql", 1)

    import psycopg2

    try:
        conn = psycopg2.connect(dsn)
    except psycopg2.OperationalError as e:
        err = str(e).lower()
        if "password authentication failed" in err:
            print(
                "Ошибка пароля PostgreSQL: пароль в DATABASE_URL не совпадает с пользователем postgres в контейнере.\n"
                f"Проверьте {env_path} и выполните из корня репозитория:\n"
                "  /opt/myvpn/.venv/bin/python scripts/fix_postgres_password.py",
                file=sys.stderr,
            )
        raise SystemExit(1) from e
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
