#!/usr/bin/env bash
# Запуск из корня репозитория (рядом с alembic.ini).
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"
if [[ ! -f alembic.ini ]]; then
  echo "Нет alembic.ini в $ROOT — скопируйте на сервер весь проект (папки alembic/, alembic.ini), не только app/"
  exit 1
fi
VENV="${ROOT}/.venv/bin/python"
if [[ ! -x "$VENV" ]]; then
  VENV="python3"
fi
exec "$VENV" -m alembic upgrade head
