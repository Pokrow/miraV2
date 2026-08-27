"""Точка входа: сборка роутеров, запуск поллинга и корректная остановка.

Сами обработчики живут в пакете handlers/ — здесь только сборка и жизненный цикл.
"""

import asyncio
import logging
import signal

from aiogram.types import BotCommand

import config
import database as db
import handlers
import tasks
from bot_core import ThrottlingMiddleware, bot, dp

logger = logging.getLogger("mira_bot")

# --- СБОРКА ---
# Один экземпляр троттлинга на оба потока событий: счётчики сообщений и кнопок
# внутри него раздельные, но чистка и безлимит администратора — общие.
#
# Именно outer_middleware, а не middleware: обычная оборачивает только
# обработчики самого диспетчера, а они все живут в дочерних роутерах —
# лимит просто не применялся бы.
throttling = ThrottlingMiddleware()
dp.message.outer_middleware(throttling)
dp.callback_query.outer_middleware(throttling)

dp.include_routers(*handlers.ROUTERS)


# --- ЗАПУСК БОТА ---
async def main():
    logger.info("🔌 Инициализация SQLite базы...")
    await db.init_db()

    # Меню команд в интерфейсе Telegram: без него человек не знает, что есть /help.
    try:
        await bot.set_my_commands([
            BotCommand(command="start", description="Начать заново"),
            BotCommand(command="help", description="Что умеет Мира"),
        ])
    except Exception as e:
        logger.warning(f"⚠️ Не удалось обновить меню команд: {e}")

    # Ежедневный бэкап и напоминания живут рядом с поллингом и гаснут вместе с ним.
    background = tasks.start_all()

    logger.info(f"✨ Мира готова к работе (Модульная архитектура, Релизная версия)!")

    # Соединение с БД закрывается ОДИН раз, при выходе из цикла. Раньше close()
    # стоял в finally внутри while: после первого же падения поллинга бот
    # поднимался снова, но уже с закрытой базой — и молча ломался на каждом
    # запросе до ручного рестарта.
    backoff = 5
    try:
        while True:
            try:
                await dp.start_polling(bot, polling_timeout=10)
                break
            except Exception as e:
                logger.error(f"Поллинг упал: {e}. Перезапуск через {backoff}с...", exc_info=True)
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2, 60)
    finally:
        # Сначала гасим фоновые задачи, потом закрываем базу: иначе бэкап
        # мог бы обратиться к уже закрытому соединению.
        await tasks.stop_all(background)
        await _shutdown()


async def _shutdown() -> None:
    """Закрывает соединения. Для SQLite в режиме WAL это ещё и чекпойнт:
    без него свежие записи остаются в mira.db-wal, и копия одного mira.db
    приезжает без последних раскладов и списаний."""
    if db.db_conn:
        try:
            await db.db_conn.execute("PRAGMA wal_checkpoint(TRUNCATE);")
            await db.db_conn.close()
            logger.info("🔌 База закрыта, WAL сброшен в основной файл.")
        except Exception as e:
            logger.error(f"Не удалось корректно закрыть базу: {e}")
    try:
        await bot.session.close()
    except Exception:
        pass


def _request_stop(signum, _frame) -> None:
    """docker stop шлёт SIGTERM, а его действие по умолчанию убивает процесс
    мимо finally — база остаётся незакрытой, WAL несброшенным. Переводим сигнал
    в KeyboardInterrupt, чтобы отработал обычный путь остановки."""
    logger.info(f"⏹ Получен сигнал {signum}, останавливаюсь...")
    raise KeyboardInterrupt


if __name__ == "__main__":
    signal.signal(signal.SIGTERM, _request_stop)
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        logger.info("👋 Мира остановлена.")
