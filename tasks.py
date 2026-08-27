"""Фоновые задачи: ежедневный бэкап и напоминание о бесплатной карте дня.

Обе задачи — вечные циклы «поспать до нужного часа, сделать дело». Ошибка
внутри цикла не должна его убивать: молча умерший бэкап хуже отсутствующего,
потому что о нём никто не узнает. Поэтому каждый прогон обёрнут, а о сбое
уходит сообщение администратору.
"""

import asyncio
import glob
import logging
import os
from datetime import datetime, timedelta

from aiogram.exceptions import TelegramForbiddenError, TelegramRetryAfter
from aiogram.types import FSInputFile

import config
import database as db
from alerts import esc, notify_admin
from bot_core import bot

logger = logging.getLogger("mira_bot")

# Telegram не принимает от бота файлы больше 50 МБ.
MAX_UPLOAD_MB = 45


async def _sleep_until(hour: int, what: str) -> None:
    """Спит до ближайшего наступления указанного часа по времени сервера."""
    now = datetime.now()
    target = now.replace(hour=hour, minute=0, second=0, microsecond=0)
    if target <= now:
        target += timedelta(days=1)
    delay = (target - now).total_seconds()
    logger.info(f"⏰ [{what}] Следующий запуск {target:%d.%m в %H:%M} (через {delay / 3600:.1f} ч).")
    await asyncio.sleep(delay)


# --- ЕЖЕДНЕВНЫЙ БЭКАП ---
def _rotate_backups() -> int:
    """Оставляет только BACKUP_KEEP свежих копий. Возвращает число удалённых."""
    files = sorted(
        glob.glob(os.path.join(config.BACKUP_DIR, "backup_mira_*.db")),
        key=os.path.getmtime,
        reverse=True,
    )
    removed = 0
    for old in files[config.BACKUP_KEEP:]:
        try:
            os.remove(old)
            removed += 1
        except OSError as e:
            logger.warning(f"⚠️ [Бэкап] Не удалось удалить {old}: {e}")
    return removed


async def run_backup(notify: bool = True) -> str:
    """Делает копию пользовательских данных и отправляет её администратору."""
    os.makedirs(config.BACKUP_DIR, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d_%H%M")
    path = os.path.join(config.BACKUP_DIR, f"backup_mira_{stamp}.db")

    counts = await asyncio.to_thread(db.create_backup_sync, path, False)
    size_mb = counts.pop("_bytes") / 1024 / 1024
    removed = _rotate_backups()

    rows = "\n".join(f"   • {t}: {c}" for t, c in counts.items())
    logger.info(f"💾 [Бэкап] {path} · {size_mb:.2f} МБ · удалено старых: {removed}")

    if not notify or not config.ADMIN_ID:
        return path

    caption = (
        f"💾 <b>Ежедневный бэкап</b>\n"
        f"{datetime.now().strftime('%d.%m.%Y %H:%M')} · {size_mb:.2f} МБ\n\n{rows}"
    )
    try:
        if size_mb > MAX_UPLOAD_MB:
            # Файл великоват для Telegram — сообщаем, что копия всё же сделана.
            await notify_admin(
                f"💾 Бэкап сделан, но {size_mb:.1f} МБ — Telegram столько не примет.\n"
                f"Файл на сервере: <code>{esc(path)}</code>",
                key="backup_too_big",
            )
        else:
            await bot.send_document(config.ADMIN_ID, FSInputFile(path), caption=caption)
    except Exception as e:
        logger.error(f"❌ [Бэкап] Не удалось отправить файл: {e}", exc_info=True)
    return path


async def daily_backup_loop() -> None:
    while True:
        await _sleep_until(config.BACKUP_HOUR, "Бэкап")
        try:
            await run_backup()
        except asyncio.CancelledError:
            raise
        except Exception as e:
            logger.error(f"❌ [Бэкап] Прогон сорвался: {e}", exc_info=True)
            await notify_admin(
                f"❌ <b>Бэкап не сделан</b>\n\n<code>{esc(e)}</code>", key="backup_failed"
            )


# --- НАПОМИНАНИЕ О БЕСПЛАТНОЙ КАРТЕ ДНЯ ---
REMINDER_TEXT = (
    "🕯️ Твоя бесплатная <b>Карта дня</b> ждёт.\n\n"
    "Загляни, если хочется услышать, о чём сегодняшний день.\n"
    "<i>Отключить напоминания можно в «🌌 Мой профиль».</i>"
)


async def send_reminders() -> tuple[int, int]:
    """Рассылает напоминание согласившимся. Возвращает (доставлено, заблокировали)."""
    recipients = await db.get_reminder_recipients()
    if not recipients:
        return 0, 0

    sent = blocked = 0
    for user_id in recipients:
        try:
            await bot.send_message(user_id, REMINDER_TEXT)
            sent += 1
            await db.mark_reminded(user_id)
        except TelegramRetryAfter as e:
            logger.warning(f"⏳ [Напоминания] Флуд-контроль, пауза {e.retry_after} сек.")
            await asyncio.sleep(e.retry_after)
        except TelegramForbiddenError:
            # Человек заблокировал бота — больше не тревожим ни здесь, ни в рассылке.
            blocked += 1
            await db.mark_blocked(user_id)
        except Exception as e:
            logger.warning(f"⚠️ [Напоминания] Ошибка для {user_id}: {e}")
        await asyncio.sleep(1 / config.REMINDER_RATE)

    logger.info(f"🔔 [Напоминания] Отправлено {sent}, заблокировали {blocked}.")
    return sent, blocked


async def daily_reminder_loop() -> None:
    while True:
        await _sleep_until(config.REMINDER_HOUR, "Напоминания")
        try:
            await send_reminders()
        except asyncio.CancelledError:
            raise
        except Exception as e:
            logger.error(f"❌ [Напоминания] Прогон сорвался: {e}", exc_info=True)
            await notify_admin(
                f"❌ <b>Напоминания не отправлены</b>\n\n<code>{esc(e)}</code>",
                key="reminders_failed",
            )


def start_all() -> list[asyncio.Task]:
    """Запускает фоновые циклы. Список нужен, чтобы погасить их при остановке."""
    return [
        asyncio.create_task(daily_backup_loop(), name="daily_backup"),
        asyncio.create_task(daily_reminder_loop(), name="daily_reminder"),
    ]


async def stop_all(background: list[asyncio.Task]) -> None:
    for task in background:
        task.cancel()
    for task in background:
        try:
            await task
        except (asyncio.CancelledError, Exception):
            pass
