"""Мелкие помощники, нужные сразу нескольким роутерам."""

import html

from aiogram.fsm.context import FSMContext
from aiogram.types import Message

import config


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


async def delete_prompt_msg(message: Message, state: FSMContext) -> None:
    data = await state.get_data()
    prompt_msg_id = data.get("prompt_msg_id")
    if prompt_msg_id:
        try:
            await message.bot.delete_message(chat_id=message.chat.id, message_id=prompt_msg_id)
        except Exception:
            pass
