#!/usr/bin/env python3
"""
Создать в контейнере Postgres базу с именем из пути DATABASE_URL (если её ещё нет).

Ошибка Alembic/systemd: «database \"myvpn\" does not exist» → выполните этот скрипт.

Запуск на VPS (из корня репозитория с .env):
  /opt/myvpn/.venv/bin/python scripts/ensure_postgres_database.py

POSTGRES_CONTAINER — имя контейнера (по умолчанию myvpn-postgres).
Если Postgres без Docker — создайте БД вручную: CREATE DATABASE myvpn;
"""
from __future__ import annotations

import os
import re
import subprocess
import sys
from pathlib import Path
from urllib.parse import urlparse


def _quote_pg_identifier(name: str) -> str:
    if re.fullmatch(r"[a-zA-Z_][a-zA-Z0-9_]*", name):
        return name
    return '"' + name.replace('"', '""') + '"'


def _database_name_from_url(parsed) -> str | None:
    path = (parsed.path or "").strip()
    name = path.strip("/").split("?", maxsplit=1)[0].strip()
    if not name:
        return None
    # только типичные имена для CREATE DATABASE (без экзотики в URL)
    if not re.fullmatch(r"[a-zA-Z_][a-zA-Z0-9_]*", name):
        print(
            f"Имя базы из DATABASE_URL не поддерживается скриптом: {name!r}. "
            "Создайте БД вручную.",
            file=sys.stderr,
        )
        return None
    return name


def main() -> int:
    root = Path(__file__).resolve().parents[1]
    env_path = root / ".env"
    if not env_path.is_file():
        print(f"Нет файла {env_path}", file=sys.stderr)
        return 1
    text = env_path.read_text(encoding="utf-8")
    m = re.search(r"^DATABASE_URL=(.*)$", text, re.MULTILINE)
    if not m:
        print("В .env не найдена строка DATABASE_URL=", file=sys.stderr)
        return 1
    raw_url = m.group(1).strip().strip('"').strip("'")
    url = raw_url.replace("postgresql+asyncpg", "postgresql", 1)
    parsed = urlparse(url)
    dbname = _database_name_from_url(parsed)
    if not dbname:
        print("Не удалось извлечь имя базы из DATABASE_URL (ожидается …/имя_бд).", file=sys.stderr)
        return 1

    container = os.environ.get("POSTGRES_CONTAINER", "myvpn-postgres")
    ident = _quote_pg_identifier(dbname)

    check = [
        "docker",
        "exec",
        container,
        "psql",
        "-U",
        "postgres",
        "-d",
        "postgres",
        "-tAc",
        f"SELECT 1 FROM pg_database WHERE datname = '{dbname}'",
    ]
    create = [
        "docker",
        "exec",
        container,
        "psql",
        "-U",
        "postgres",
        "-d",
        "postgres",
        "-c",
        f"CREATE DATABASE {ident}",
    ]

    print(f"Проверяю базу {dbname!r} в контейнере {container} …", flush=True)
    try:
        r = subprocess.run(check, check=False, capture_output=True, text=True)
        if r.returncode != 0:
            print(r.stderr or r.stdout or "pg query failed", file=sys.stderr)
            return r.returncode or 1
        if r.stdout.strip() == "1":
            print(f"База {dbname} уже есть. Перезапуск: systemctl restart myvpn-bot")
            return 0
        print(f"Создаю базу {dbname} …", flush=True)
        subprocess.run(create, check=True)
    except FileNotFoundError:
        print("Команда docker не найдена. Создайте базу вручную на вашем Postgres.", file=sys.stderr)
        return 1
    except subprocess.CalledProcessError as e:
        print(f"docker exec завершился с кодом {e.returncode}", file=sys.stderr)
        return e.returncode or 1

    print("Готово. Запустите: systemctl restart myvpn-bot")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
