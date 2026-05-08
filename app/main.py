import asyncio

from aiogram import Bot, Dispatcher
from aiogram.fsm.storage.redis import RedisStorage

from app.config import settings
from app.handlers import admin, payments, user
from app.logging_setup import setup_logging
from app.scheduler import background_jobs
from app.subscription_gate_http import start_subscription_gate_server, stop_subscription_gate_server


async def main() -> None:
    setup_logging()
    await start_subscription_gate_server()
    bot = Bot(token=settings.bot_token)
    storage = RedisStorage.from_url(settings.redis_url)
    dp = Dispatcher(storage=storage)

    dp.include_router(user.router)
    dp.include_router(payments.router)
    dp.include_router(admin.router)

    asyncio.create_task(background_jobs(bot))
    try:
        await dp.start_polling(bot)
    finally:
        await storage.close()
        await stop_subscription_gate_server()


if __name__ == "__main__":
    asyncio.run(main())
