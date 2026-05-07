#!/usr/bin/env python3
"""
Сброс флага trial_used для всех пользователей в БД бота (не трогает Marzban).

Опционально удаляет все записи subscriptions в боте (--wipe-subs).

  /opt/myvpn/.venv/bin/python scripts/reset_all_trials.py --dry-run
  /opt/myvpn/.venv/bin/python scripts/reset_all_trials.py --apply
  /opt/myvpn/.venv/bin/python scripts/reset_all_trials.py --apply --wipe-subs
"""
from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path


def main() -> int:
    p = argparse.ArgumentParser(description="Сброс пробных периодов для всех пользователей")
    p.add_argument("--dry-run", action="store_true", help="Показать счётчики без изменений")
    p.add_argument("--apply", action="store_true", help="Применить изменения")
    p.add_argument(
        "--wipe-subs",
        action="store_true",
        help="Также DELETE FROM subscriptions (очистка записей подписок в боте)",
    )
    args = p.parse_args()
    if not args.apply and not args.dry_run:
        print("Укажите --dry-run и/или --apply", file=sys.stderr)
        return 1

    try:
        from dotenv import load_dotenv
    except ImportError:
        load_dotenv = None  # type: ignore[assignment]
    env_path = Path(__file__).resolve().parents[1] / ".env"
    if load_dotenv:
        load_dotenv(env_path, override=True)

    raw_url = os.getenv("DATABASE_URL", "").strip()
    if not raw_url:
        print("Нет DATABASE_URL", file=sys.stderr)
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
            cur.execute("SELECT COUNT(*) FROM users")
            n_users = cur.fetchone()[0]
            cur.execute("SELECT COUNT(*) FROM users WHERE trial_used = true")
            n_flagged = cur.fetchone()[0]
            cur.execute("SELECT COUNT(*) FROM subscriptions")
            n_subs = cur.fetchone()[0]
        print(f"Пользователей в users: {n_users}, с trial_used=true: {n_flagged}, подписок: {n_subs}")

        if args.dry_run and not args.apply:
            print("Dry-run: изменений нет.")
            return 0

        if not args.apply:
            return 0

        with conn.cursor() as cur:
            if args.wipe_subs:
                cur.execute("DELETE FROM subscriptions")
                print(f"Удалено строк subscriptions: {cur.rowcount}")
            cur.execute("UPDATE users SET trial_used = false")
            print(f"Обновлено пользователей (trial_used=false): {cur.rowcount}")
        print("Готово. Пользователей Marzban при необходимости чистите в панели вручную.")
    finally:
        conn.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
