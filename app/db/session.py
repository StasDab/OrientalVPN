from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.config import settings


engine = create_async_engine(
    settings.database_url,
    echo=False,
    future=True,
    pool_pre_ping=True,
    # Убираем "зависшие" idle-соединения к Postgres после долгого простоя.
    pool_recycle=1800,
)
SessionLocal = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
