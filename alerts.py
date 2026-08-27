"""Оповещения администратора о сбоях.

Логи хороши для разбора, но о них узнаёшь, только если открыть файл. Сюда
уходит то, о чём нужно узнать сразу: необработанные исключения и сорвавшиеся
фоновые задачи.
"""

import html
import logging
import time

import config
from bot_core import bot

logger = logging.getLogger("mira_bot")

# Время последней отправки по каждому ключу: одна зациклившаяся ошибка иначе
# зальёт личку сотнями одинаковых сообщений за минуту.
_last_sent: dict[str, float] = {}


async def notify_admin(text: str, key: str | None = None) -> None:
    """Шлёт сообщение администратору не чаще раза в ADMIN_ALERT_COOLDOWN на ключ.

    key задаёт, что считать «той же самой» ошибкой. По умолчанию — начало текста.
    """
    if not config.ADMIN_ID:
        return

    now = time.time()
    dedup_key = key or text[:80]
    if now - _last_sent.get(dedup_key, 0.0) < config.ADMIN_ALERT_COOLDOWN_SECONDS:
        return

    if len(_last_sent) > 200:
        cutoff = now - config.ADMIN_ALERT_COOLDOWN_SECONDS
        for k in [k for k, t in _last_sent.items() if t < cutoff]:
            del _last_sent[k]
    _last_sent[dedup_key] = now

    try:
        # 4096 — предел Telegram; режем с запасом на разметку.
        await bot.send_message(config.ADMIN_ID, text[:3800])
    except Exception as e:
        logger.warning(f"⚠️ [Alert] Не удалось уведомить администратора: {e}")


def esc(text: str) -> str:
    """Экранирование для HTML-разметки: в тексте исключения бывают < и >."""
    return html.escape(str(text))
