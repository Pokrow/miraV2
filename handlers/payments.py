"""Баланс, пополнение свечами, приём платежей и реферальная программа."""

import logging

from aiogram import F, Router
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, LabeledPrice, Message, PreCheckoutQuery

import config
import database as db
import keyboards as kb
from bot_core import bot
from utils import _esc, _fmt_dt, _rub_enabled, delete_prompt_msg

logger = logging.getLogger("mira_bot")
router = Router(name="payments")


# --- БАЛАНС И ПОПОЛНЕНИЕ ---
async def _balance_text(user_id: int) -> str:
    balance = await db.get_balance(user_id)
    prices = db.get_all_prices()
    price_lines = "\n".join(f"   • {name} — <b>{p}</b> 🕯️" for name, p in prices.items())
    free_card = "уже взята сегодня" if not await db.free_card_available(user_id) else "доступна сегодня"
    return (
        f"🕯️ <b>Твой баланс: {balance} свечей</b>\n\n"
        f"Каждая свеча — это вопрос к картам. Стоимость:\n{price_lines}\n\n"
        f"<b>Бесплатно:</b>\n"
        f"   • 🃏 Первая «Карта дня» каждый день — <i>{free_card}</i>\n"
        f"   • 💬 Одно уточнение к каждому раскладу\n\n"
        f"<i>Свечи не сгорают и не имеют срока действия.</i>"
    )


@router.message(F.text == "🕯️ Баланс")
async def hub_balance(message: Message, state: FSMContext):
    await delete_prompt_msg(message, state)
    await state.clear()
    await message.answer(
        await _balance_text(message.from_user.id), reply_markup=kb.get_balance_keyboard()
    )


@router.callback_query(F.data == "show_topup")
async def show_topup(callback: CallbackQuery):
    await callback.answer()
    packages = await db.get_topup_packages()
    if not packages:
        return await callback.message.answer("Пополнение временно недоступно 🕯️")

    balance = await db.get_balance(callback.from_user.id)
    pay_hint = (
        "Оплата звёздами Telegram или банковской картой."
        if _rub_enabled()
        else "Оплата звёздами Telegram."
    )
    await callback.message.answer(
        f"🕯️ <b>Пополнение баланса</b>\nСейчас у тебя: <b>{balance}</b> свечей\n\n{pay_hint}",
        reply_markup=kb.get_topup_keyboard(packages, show_rub=_rub_enabled()),
    )


@router.callback_query(F.data == "show_history_tx")
async def show_history_tx(callback: CallbackQuery):
    await callback.answer()
    items = await db.get_transactions(callback.from_user.id, limit=15)
    if not items:
        return await callback.message.answer("Пока никаких движений по балансу 🕯️")

    lines = []
    for t in items:
        sign = "+" if t["amount"] > 0 else ""
        lines.append(
            f"{_fmt_dt(t['created_at'])} · <b>{sign}{t['amount']}</b> 🕯️ "
            f"→ {t['balance_after']}\n   <i>{_esc(t['note'] or t['kind'])}</i>"
        )
    await callback.message.answer(
        "📖 <b>История баланса</b>\n\n" + "\n".join(lines),
        reply_markup=kb.get_balance_keyboard(),
    )


@router.callback_query(F.data.startswith("pay_stars_") | F.data.startswith("pay_rub_"))
async def create_topup_invoice(callback: CallbackQuery):
    """Выставляет счёт: звёздами (provider_token пустой) или через провайдера."""
    await callback.answer()
    is_stars = callback.data.startswith("pay_stars_")
    package_id = int(callback.data.rsplit("_", 1)[1])

    pkg = await db.get_topup_package(package_id)
    if not pkg or not pkg["active"]:
        return await callback.message.answer("Этот пакет больше недоступен 🕯️")

    if is_stars:
        provider_token, currency = "", "XTR"
        # Для XTR amount — это количество звёзд.
        prices = [LabeledPrice(label=f"{pkg['candles']} свечей", amount=pkg["stars"])]
    else:
        if not _rub_enabled():
            return await callback.message.answer("Оплата картой временно недоступна 🕯️")
        provider_token, currency = config.PAYMENT_PROVIDER_TOKEN, config.PAYMENT_CURRENCY
        # Обычные валюты Telegram принимает в минимальных единицах (копейках).
        prices = [LabeledPrice(label=f"{pkg['candles']} свечей", amount=pkg["rub"] * 100)]

    await callback.message.answer_invoice(
        title=f"🕯️ {pkg['candles']} свечей",
        description=f"{pkg['title']} — пополнение баланса в боте «Мира».",
        payload=f"topup_{pkg['id']}",
        provider_token=provider_token,
        currency=currency,
        prices=prices,
    )


@router.pre_checkout_query()
async def process_pre_checkout(query: PreCheckoutQuery):
    """Telegram ждёт ответ не дольше 10 секунд, иначе платёж отменяется."""
    payload = query.invoice_payload or ""
    if not payload.startswith("topup_"):
        return await query.answer(ok=False, error_message="Неизвестный платёж 🕯️")

    pkg = await db.get_topup_package(int(payload.removeprefix("topup_")))
    if not pkg:
        return await query.answer(ok=False, error_message="Этот пакет больше недоступен 🕯️")
    await query.answer(ok=True)


@router.message(F.successful_payment)
async def process_successful_payment(message: Message, state: FSMContext):
    """Зачисляет свечи после успешной оплаты."""
    await state.clear()
    sp = message.successful_payment
    user_id = message.from_user.id
    pkg = await db.get_topup_package(int((sp.invoice_payload or "topup_0").removeprefix("topup_")))

    if not pkg:
        logger.error(
            f"❌ [Оплата] Неизвестный пакет '{sp.invoice_payload}' от {user_id}, "
            f"charge={sp.telegram_payment_charge_id}"
        )
        return await message.answer(
            "Оплата получена, но пакет не распознан 🕯️ Напиши администратору — разберёмся и вернём деньги.",
            reply_markup=kb.get_main_keyboard(),
        )

    is_stars = sp.currency == "XTR"
    # Защита от двойного зачисления, если Telegram повторит апдейт об оплате.
    is_new = await db.record_payment(
        user_id,
        sp.telegram_payment_charge_id,
        sp.total_amount,
        sp.currency,
        pkg["candles"],
        "stars" if is_stars else "provider",
    )
    if not is_new:
        return

    balance = await db.add_candles(
        user_id, pkg["candles"], kind="topup", note=f"Пополнение: {pkg['title']}"
    )
    paid = f"{sp.total_amount} ⭐" if is_stars else f"{sp.total_amount / 100:.0f} {sp.currency}"
    logger.info(f"💰 [Оплата] {user_id} купил {pkg['candles']} свечей за {paid}")

    await message.answer(
        f"🕯️ <b>Баланс пополнен!</b>\n\n"
        f"Зачислено: <b>+{pkg['candles']}</b> свечей\n"
        f"Твой баланс: <b>{balance}</b> свечей\n\n"
        f"<i>Спасибо за доверие 🤍</i>",
        reply_markup=kb.get_main_keyboard(),
    )

    if config.ADMIN_ID:
        try:
            await bot.send_message(
                config.ADMIN_ID,
                f"💰 Оплата: <code>{user_id}</code> · {pkg['candles']} 🕯️ · {paid}",
            )
        except Exception:
            pass


# --- РЕФЕРАЛЬНАЯ ПРОГРАММА ---
@router.callback_query(F.data == "referral_info")
async def referral_info(callback: CallbackQuery):
    await callback.answer()
    await _send_referral_card(callback.message, callback.from_user.id)


async def _send_referral_card(message: Message, user_id: int) -> None:
    me = await bot.me()
    link = f"https://t.me/{me.username}?start=ref{user_id}"
    stats = await db.get_referral_stats(user_id)
    bonus = db.get_setting_int("referral_bonus", config.DEFAULT_REFERRAL_BONUS)

    await message.answer(
        f"🎁 <b>Приглашай друзей — получай свечи</b>\n\n"
        f"За каждого друга, который придёт по твоей ссылке и сделает первый расклад, "
        f"ты получаешь <b>{bonus}</b> свечей 🕯️\n\n"
        f"👥 Пришли по твоей ссылке: <b>{stats['invited']}</b>\n"
        f"✨ Засчитано наград: <b>{stats['rewarded']}</b>\n\n"
        f"Твоя ссылка:\n<code>{link}</code>",
        reply_markup=kb.get_referral_keyboard(link),
    )
