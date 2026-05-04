from __future__ import annotations

import os
from logging.config import fileConfig

from alembic import context
from sqlalchemy import create_engine, pool

from app.db.models import Base

config = context.config

if config.config_file_name is not None:
    fileConfig(config.config_file_name)

target_metadata = Base.metadata


def _migration_database_url() -> str:
    """Alembic миграции идут через синхронный драйвер (asyncpg в рантайме приложения)."""
    raw = os.getenv("DATABASE_URL", "").strip()
    if raw:
        if "+asyncpg" in raw:
            return raw.replace("postgresql+asyncpg", "postgresql+psycopg2", 1)
        return raw
    return config.get_main_option("sqlalchemy.url")


def run_migrations_offline() -> None:
    url = _migration_database_url()
    context.configure(
        url=url,
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
    )
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    url = _migration_database_url()
    connectable = create_engine(url, poolclass=pool.NullPool)

    with connectable.connect() as connection:
        context.configure(connection=connection, target_metadata=target_metadata)
        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
