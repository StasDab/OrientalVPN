#!/usr/bin/env python3
"""
Выставить в контейнере Postgres пароль роли такой же, как в DATABASE_URL в .env
(имя роли берётся из URL: user:password@host, не только postgres).

Запуск на VPS (из корня репозитория):

  cd /opt/myvpn && .venv/bin/python scripts/fix_postgres_password.py

Читается `.env` в корне или `app/.env`.

Переменная окружения POSTGRES_CONTAINER — имя контейнера (по умолчанию myvpn-postgres).
"""
from __future__ import annotations

import os
import re
import subprocess
import sys
from pathlib import Path
from urllib.parse import unquote_plus, urlparse


def _escape_sql_literal(s: str) -> str:
    return s.replace("'", "''")


def _quote_pg_identifier(name: str) -> str:
    """Безопасное имя роли для ALTER USER … (простые идентификаторы или в кавычках)."""
    if re.fullmatch(r"[a-zA-Z_][a-zA-Z0-9_]*", name):
        return name
    return '"' + name.replace('"', '""') + '"'


def main() -> int:
    root = Path(__file__).resolve().parents[1]
    env_path = root / ".env"
    if not env_path.is_file():
        env_path = root / "app" / ".env"
    if not env_path.is_file():
        print(f"Нет файла {root / '.env'} ни {root / 'app' / '.env'}", file=sys.stderr)
        return 1
    text = env_path.read_text(encoding="utf-8")
    m = re.search(r"^DATABASE_URL=(.*)$", text, re.MULTILINE)
    if not m:
        print("В .env не найдена строка DATABASE_URL=", file=sys.stderr)
        return 1
    raw_url = m.group(1).strip().strip('"').strip("'")
    url = raw_url.replace("postgresql+asyncpg", "postgresql", 1)
    parsed = urlparse(url)
    if not parsed.password:
        print("В DATABASE_URL нет пароля (user:pass@)", file=sys.stderr)
        return 1
    pw = unquote_plus(parsed.password)
    db_user = unquote_plus(parsed.username) if parsed.username else "postgres"
    container = os.environ.get("POSTGRES_CONTAINER", "myvpn-postgres")
    ident = _quote_pg_identifier(db_user)
    sql = f"ALTER USER {ident} WITH PASSWORD '{_escape_sql_literal(pw)}';"
    cmd = ["docker", "exec", container, "psql", "-U", "postgres", "-d", "postgres", "-c", sql]
    print(
        f"Обновляю пароль роли {db_user!r} в контейнере {container} по паролю из DATABASE_URL …",
        flush=True,
    )
    try:
        subprocess.run(cmd, check=True)
    except FileNotFoundError:
        print("Команда docker не найдена. Установите Docker или выполните ALTER вручную.", file=sys.stderr)
        return 1
    except subprocess.CalledProcessError as e:
        print(f"docker exec завершился с кодом {e.returncode}", file=sys.stderr)
        return e.returncode or 1
    print("Готово. Запустите: systemctl restart myvpn-bot")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
