"""Мелкие помощники, нужные сразу нескольким роутерам."""

import html
import logging
import re

from aiogram.exceptions import TelegramBadRequest
from aiogram.fsm.context import FSMContext
from aiogram.types import InlineKeyboardMarkup, Message

import config

logger = logging.getLogger("mira_bot")

# Предел Telegram — 4096 символов. Режем с запасом: разметка при разрыве
# дополняется закрывающими тегами, и они тоже занимают место.
MESSAGE_LIMIT = 3900


def _rub_enabled() -> bool:
    """Оплата рублями доступна, только если подключён провайдер Telegram Payments."""
    return bool(config.PAYMENT_PROVIDER_TOKEN)


def is_admin(user_id: int) -> bool:
    return config.ADMIN_ID != 0 and user_id == config.ADMIN_ID


def _esc(text: str | None) -> str:
    """Экранирует пользовательский текст: вопросы могут содержать < > & и ломать HTML-разметку."""
    return html.escape(text or "")


def _fmt_dt(raw: str | None) -> str:
    """'2026-08-22 20:27:34' -> '22.08 20:27'."""
    if not raw or len(raw) < 16:
        return raw or "—"
    return f"{raw[8:10]}.{raw[5:7]} {raw[11:16]}"


def split_message(text: str, limit: int = MESSAGE_LIMIT) -> list[str]:
    """Режет длинный текст на части по границам абзацев.

    Разрыв ищется сначала по пустой строке, потом по переводу строки, потом по
    пробелу — рвать слово посреди фразы в ответе таролога некрасиво.
    """
    if len(text) <= limit:
        return [text]

    parts: list[str] = []
    rest = text
    while len(rest) > limit:
        window = rest[:limit]
        cut = window.rfind("\n\n")
        if cut < limit // 2:
            cut = window.rfind("\n")
        if cut < limit // 2:
            cut = window.rfind(" ")
        if cut <= 0:
            cut = limit
        parts.append(rest[:cut].rstrip())
        rest = rest[cut:].lstrip()
    if rest:
        parts.append(rest)
    return parts


async def answer_long(
    message: Message,
    text: str,
    reply_markup: InlineKeyboardMarkup | None = None,
) -> None:
    """Отправляет текст любой длины, а при поломке разметки — как простой текст.

    Три уровня страховки: нарезка по лимиту Telegram, повтор без HTML-тегов
    и, в самом конце, короткое честное сообщение. Раньше ответ длиннее 4096
    символов не доходил вообще: расклад падал, свечи возвращались, а человек
    оставался без трактовки.
    """
    chunks = split_message(text)
    for i, chunk in enumerate(chunks):
        # Кнопки вешаются только на последнюю часть, иначе они повторятся.
        markup = reply_markup if i == len(chunks) - 1 else None
        try:
            await message.answer(chunk, reply_markup=markup)
        except TelegramBadRequest as e:
            logger.warning(f"⚠️ Telegram отверг разметку ({e}). Отправляю без тегов.")
            plain = re.sub(r"<[^>]+>", "", chunk)
            try:
                await message.answer(plain, reply_markup=markup)
            except TelegramBadRequest:
                logger.error("❌ Не удалось отправить даже очищенный текст.", exc_info=True)
                await message.answer(
                    "Ответ получился слишком длинным, и я не смогла его показать 🕯️ "
                    "Попробуй задать вопрос чуть конкретнее.",
                    reply_markup=markup,
                )


async def delete_prompt_msg(message: Message, state: FSMContext) -> None:
    data = await state.get_data()
    prompt_msg_id = data.get("prompt_msg_id")
    if prompt_msg_id:
        try:
            await message.bot.delete_message(chat_id=message.chat.id, message_id=prompt_msg_id)
        except Exception:
            pass
