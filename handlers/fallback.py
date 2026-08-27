"""Последний рубеж: сообщения мимо сценария и необработанные исключения.

Роутер включается последним — его catch-all перехватил бы всё остальное.
"""

import logging

from aiogram import F, Router
from aiogram.types import Message
from aiogram.types.error_event import ErrorEvent

import database as db
import keyboards as kb
from alerts import esc, notify_admin

logger = logging.getLogger("mira_bot")
router = Router(name="fallback")


@router.message(F.text)
async def handle_regular_chat(message: Message):
    # Сообщения «мимо» сценария тоже попадают в журнал: без них в админке
    # не видно, что человек вообще писал и чего не понял в меню.
    await db.log_request(
        message.from_user.id, "Свободный текст", message.text or "",
        status="rejected", note="Сообщение вне сценария расклада",
    )
    await message.answer("Я работаю с картами и внутренними состояниями 🕯️\n\nВыбери нужный раздел в меню ниже 👇",
                         reply_markup=kb.get_main_keyboard())


@router.message(~F.text)
async def handle_non_text(message: Message):
    await message.answer("Я умею слушать слова и читать язык карт 🕯️ Выбери нужную кнопку в меню ниже.")


@router.error()
async def global_error_handler(event: ErrorEvent):
    logger.error(f"⚠️ Необработанное исключение: {event.exception}", exc_info=True)

    # Сообщение админу: тип исключения задаёт ключ подавления повторов, поэтому
    # шквал одинаковых ошибок даст одно письмо, а не сотню.
    exc = event.exception
    update = event.update
    who = getattr(getattr(update, "message", None) or getattr(update, "callback_query", None),
                  "from_user", None)
    what = getattr(getattr(update, "message", None), "text", None) or \
        getattr(getattr(update, "callback_query", None), "data", None) or "—"
    await notify_admin(
        f"⚠️ <b>Необработанная ошибка</b>\n\n"
        f"<code>{esc(type(exc).__name__)}: {esc(exc)}</code>\n\n"
        f"👤 <code>{who.id if who else '—'}</code>\n"
        f"💬 {esc(str(what)[:200])}",
        key=f"{type(exc).__name__}:{str(exc)[:120]}",
    )

    if event.update and event.update.message:
        try:
            await event.update.message.answer(
                "Связь ненадолго стала туманной... 🕯️ Попробуй повторить действие через пару секунд.",
                reply_markup=kb.get_main_keyboard())
        except Exception:
            pass
