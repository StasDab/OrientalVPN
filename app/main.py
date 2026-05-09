import asyncio
import logging

from aiogram import Bot, Dispatcher
from aiogram.fsm.storage.redis import RedisStorage

from app.config import settings
from app.handlers import admin, payments, user
from app.logging_setup import setup_logging
from app.scheduler import background_jobs
from app.subscription_gate_http import start_subscription_gate_server, stop_subscription_gate_server


# Явно запрашиваем типы апдейтов: иначе при неверном авто-определении клиент «молчит»
# (не приходят callback_query / message с оплатой).
POLLING_ALLOWED_UPDATES = [
    "message",
    "edited_message",
    "callback_query",
    "pre_checkout_query",
]


async def main() -> None:
    setup_logging()
    _log = logging.getLogger(__name__)
    if not settings.vpn_nodes:
        _log.warning(
            "vpn_nodes пуст: проверьте VPN_NODES_JSON или VPN_NODES_JSON_FILE — иначе недоступны /give_sub, пробный и т.п."
        )
    await start_subscription_gate_server()
    bot = Bot(token=settings.bot_token)
    storage = RedisStorage.from_url(settings.redis_url)
    dp = Dispatcher(storage=storage)

    dp.include_router(user.router)
    dp.include_router(payments.router)
    dp.include_router(admin.router)

    asyncio.create_task(background_jobs(bot))
    try:
        await dp.start_polling(bot, allowed_updates=POLLING_ALLOWED_UPDATES)
    finally:
        await storage.close()
        await stop_subscription_gate_server()


if __name__ == "__main__":
    asyncio.run(main())
