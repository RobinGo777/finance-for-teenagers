import asyncio
import contextlib
import logging
import os
from aiogram import Bot, Dispatcher
from aiogram.enums import ParseMode
from aiogram.client.default import DefaultBotProperties

from config import TELEGRAM_BOT_TOKEN
from bot.moderator import router as moderator_router
from bot.publisher import bot
from scheduler.daily_scheduler import setup_scheduler
from scheduler.monitor import start_monitor

# ─────────────────────────────────────────
# ЛОГУВАННЯ
# ─────────────────────────────────────────

class _RedactSecretsFilter(logging.Filter):
    """Не пускає API-ключі з query string у логи (навіть через httpx/traceback)."""

    @staticmethod
    def _redact_value(value: object) -> object:
        from utils.http_safe import redact_secrets

        if isinstance(value, str):
            return redact_secrets(value)
        # httpx кладе URL-об'єкт у args: HTTP Request: GET <URL>
        text = str(value)
        redacted = redact_secrets(text)
        return redacted if redacted != text else value

    def filter(self, record: logging.LogRecord) -> bool:
        if isinstance(record.msg, str):
            record.msg = self._redact_value(record.msg)  # type: ignore[assignment]
        if record.args:
            if isinstance(record.args, dict):
                record.args = {
                    k: self._redact_value(v) for k, v in record.args.items()
                }
            else:
                record.args = tuple(self._redact_value(a) for a in record.args)
        return True


logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
logging.getLogger().addFilter(_RedactSecretsFilter())
logger = logging.getLogger(__name__)


async def start_keepalive_server() -> None:
    """Minimal HTTP server for Render Web Service health checks."""
    host = "0.0.0.0"
    port = int(os.getenv("PORT", "10000"))

    async def _handler(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        try:
            await reader.read(1024)
            body = b"ok"
            response = (
                b"HTTP/1.1 200 OK\r\n"
                b"Content-Type: text/plain; charset=utf-8\r\n"
                b"Content-Length: 2\r\n"
                b"Connection: close\r\n"
                b"\r\n"
                + body
            )
            writer.write(response)
            await writer.drain()
        finally:
            writer.close()
            await writer.wait_closed()

    server = await asyncio.start_server(_handler, host, port)
    logger.info("🌐 Keep-alive HTTP сервер запущено на %s:%s", host, port)
    async with server:
        await server.serve_forever()


# ─────────────────────────────────────────
# ГОЛОВНА ФУНКЦІЯ
# ─────────────────────────────────────────

async def main() -> None:
    logger.info("🚀 Запуск ФінПро бота...")

    # Dispatcher
    dp = Dispatcher()
    dp.include_router(moderator_router)

    # Scheduler
    scheduler = setup_scheduler()
    scheduler.start()
    logger.info("📅 Розклад публікацій запущено")

    # Monitor (реалтайм) — запускаємо як окрему задачу
    monitor_task = asyncio.create_task(start_monitor())
    logger.info("📡 Реалтайм моніторинг запущено")

    # Keep-alive HTTP server для Render Web Service
    keepalive_task = asyncio.create_task(start_keepalive_server())

    # Polling
    logger.info("✅ Бот готовий до роботи!")
    try:
        await dp.start_polling(bot, allowed_updates=["message", "callback_query", "poll_answer"])
    finally:
        from data.redis_client import close as close_redis
        from generators.gemini import close as close_gemini
        from data.fetchers import close as close_fetchers

        scheduler.shutdown()
        monitor_task.cancel()
        keepalive_task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await keepalive_task
        await bot.session.close()
        await close_gemini()
        await close_fetchers()
        await close_redis()
        logger.info("🛑 Бот зупинено")


if __name__ == "__main__":
    asyncio.run(main())
