import asyncio

from aiogram import Bot, Dispatcher
from aiogram.fsm.storage.memory import MemoryStorage

from app.config import settings
from app.handlers import admin, payments, user
from app.logging_setup import setup_logging
from app.scheduler import background_jobs


async def main() -> None:
    setup_logging()
    bot = Bot(token=settings.bot_token)
    dp = Dispatcher(storage=MemoryStorage())

    dp.include_router(user.router)
    dp.include_router(payments.router)
    dp.include_router(admin.router)

    asyncio.create_task(background_jobs(bot))
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
