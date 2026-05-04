"""Все сроки в БД и для Marzban — UTC. Наивный datetime в Postgres = UTC wall clock."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone


def utc_now_naive() -> datetime:
    """Текущий момент как naive datetime в компонентах UTC (как раньше utcnow())."""
    return datetime.now(timezone.utc).replace(tzinfo=None)


def utc_timestamp_after(delta: timedelta) -> int:
    """Unix-секунды (UTC) для момента now(UTC)+delta — для Marzban expire."""
    return int((datetime.now(timezone.utc) + delta).timestamp())


def naive_utc_from_timestamp(ts: int) -> datetime:
    """Naive UTC из unix-секунд."""
    return datetime.fromtimestamp(ts, tz=timezone.utc).replace(tzinfo=None)


def naive_utc_timestamp(dt: datetime) -> int:
    """Unix-секунды для naive UTC (или aware → UTC). Иначе .timestamp() ломается при TZ сервера ≠ UTC."""
    if dt.tzinfo is not None:
        return int(dt.astimezone(timezone.utc).timestamp())
    return int(dt.replace(tzinfo=timezone.utc).timestamp())
