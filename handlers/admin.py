"""Админ-панель: лента обращений, статистика, оплаты, цены, рассылка, бэкап."""

import asyncio
import logging
import math
import os
import re
from datetime import datetime

from aiogram import F, Router
from aiogram.exceptions import TelegramBadRequest, TelegramForbiddenError, TelegramRetryAfter
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, FSInputFile, InlineKeyboardMarkup, Message

import config
import database as db
import keyboards as kb
from bot_core import UserState, bot
from utils import _esc, _fmt_dt, _rub_enabled, is_admin

logger = logging.getLogger("mira_bot")
router = Router(name="admin")


# --- АДМИН ПАНЕЛЬ ---
async def _resolve_scope(scope: str, state: FSMContext) -> tuple[int | None, str | None, str]:
    """scope -> (user_id, search, человекочитаемый заголовок)."""
    if scope.startswith("u"):
        uid = int(scope[1:])
        return uid, None, f"👤 Запросы пользователя <code>{uid}</code>"
    if scope == "s":
        term = (await state.get_data()).get("admin_search_term", "")
        return None, term, f"🔎 Поиск: «{_esc(term)}»"
    return None, None, "📋 Все запросы пользователей"


_STATUS_MARKS = {"ok": "", "rejected": "🚧 отклонён фильтром", "error": "⚠️ сбой генерации"}


async def _render_feed(scope: str, page: int, state: FSMContext) -> tuple[str, InlineKeyboardMarkup]:
    """Собирает текст ленты запросов и клавиатуру навигации.

    Источник — журнал обращений: он содержит и расклады, и уточнения,
    и совместимость, и вопросы, отсеянные фильтром.
    """
    user_id, search, title = await _resolve_scope(scope, state)
    total = await db.count_requests(user_id=user_id, search=search)

    if not total:
        return f"{title}\n\nПока ничего не найдено 🕯️", kb.get_admin_back_keyboard()

    total_pages = max(1, math.ceil(total / db.ADMIN_PAGE_SIZE))
    page = max(0, min(page, total_pages - 1))
    items = await db.get_requests_page(page=page, user_id=user_id, search=search)

    lines = [f"{title}\n<i>Всего: {total}</i>\n"]
    for it in items:
        q = _esc(it["question"])
        if len(q) > 180:
            q = q[:180] + "…"
        status = _STATUS_MARKS.get(it["status"], it["status"])
        status_line = f" · {status}" if status else ""
        lines.append(
            f"<b>#{it['id']}</b> · {_fmt_dt(it['created_at'])} · <code>{it['user_id']}</code>\n"
            f"<i>{_esc(it['kind'])}</i>{status_line}\n"
            f"💬 {q}\n"
        )
    return "\n".join(lines), kb.get_admin_feed_keyboard(items, page, total_pages, scope)


@router.message(F.text == "/admin")
async def cmd_admin(message: Message, state: FSMContext):
    if not is_admin(message.from_user.id):
        return  # Игнорируем обычных пользователей
    await state.clear()
    await message.answer("Панель управления Мирой ⚙️", reply_markup=kb.get_admin_keyboard())


@router.callback_query(F.data == "admin_home")
async def admin_home(callback: CallbackQuery, state: FSMContext):
    if not is_admin(callback.from_user.id):
        return await callback.answer()
    await callback.answer()
    await state.clear()
    await callback.message.edit_text("Панель управления Мирой ⚙️", reply_markup=kb.get_admin_keyboard())


@router.callback_query(F.data == "admin_noop")
async def admin_noop(callback: CallbackQuery):
    await callback.answer()


@router.callback_query(F.data.startswith("admin_feed_"))
async def admin_feed(callback: CallbackQuery, state: FSMContext):
    if not is_admin(callback.from_user.id):
        return await callback.answer()
    await callback.answer()
    page = int(callback.data.rsplit("_", 1)[1])
    await state.update_data(admin_scope="all", admin_page=page)
    text, markup = await _render_feed("all", page, state)
    await callback.message.edit_text(text, reply_markup=markup)


@router.callback_query(F.data.startswith("admin_page_"))
async def admin_page(callback: CallbackQuery, state: FSMContext):
    if not is_admin(callback.from_user.id):
        return await callback.answer()
    await callback.answer()
    scope, page = callback.data[len("admin_page_"):].rsplit("_", 1)
    page = int(page)
    await state.update_data(admin_scope=scope, admin_page=page)
    text, markup = await _render_feed(scope, page, state)
    try:
        await callback.message.edit_text(text, reply_markup=markup)
    except TelegramBadRequest:
        await callback.message.answer(text, reply_markup=markup)


@router.callback_query(F.data.startswith("admin_view_"))
async def admin_view(callback: CallbackQuery, state: FSMContext):
    """Полная карточка запроса: вопрос, карты, ответ Миры и профиль автора."""
    if not is_admin(callback.from_user.id):
        return await callback.answer()
    await callback.answer()

    req = await db.get_request_by_id(int(callback.data.rsplit("_", 1)[1]))
    if not req:
        return await callback.message.answer("Запрос не найден 🕯️", reply_markup=kb.get_admin_back_keyboard())

    author_id = req["user_id"]
    card = await db.get_user_card(author_id)
    profile = ""
    if card:
        arcana = f" · {_esc(card['birth_arcana'])}" if card["birth_arcana"] else ""
        profile = f"\n👤 <code>{author_id}</code>{arcana} · раскладов: {card['spreads']}"

    data = await state.get_data()
    scope = data.get("admin_scope", "all")
    page = data.get("admin_page", 0)

    status = _STATUS_MARKS.get(req["status"], req["status"])
    status_line = f"\n{status}" if status else ""
    note_line = f"\n<i>{_esc(req['note'])}</i>" if req["note"] else ""

    # Ответ и карты есть только у состоявшегося расклада; у отклонённых
    # обращений — только сам вопрос и причина отказа.
    body = ""
    if req["spread_id"]:
        rec = await db.get_spread_by_id(req["spread_id"])
        if rec:
            body = (
                f"\n\n🃏 <b>Карты:</b>\n{_esc(rec['cards_text'])}"
                f"\n\n🕯️ <b>Ответ Миры:</b>\n{rec['answer_text']}"
            )

    text = (
        f"📜 <b>Запрос #{req['id']}</b> · {_fmt_dt(req['created_at'])}{profile}\n"
        f"<i>{_esc(req['kind'])}</i>{status_line}{note_line}\n\n"
        f"❓ <b>Вопрос:</b>\n{_esc(req['question'])}{body}"
    )
    if len(text) > 4000:
        text = text[:3990] + "\n\n<i>…обрезано</i>"

    markup = kb.get_admin_entry_keyboard(author_id, scope, page)
    try:
        await callback.message.answer(text, reply_markup=markup)
    except TelegramBadRequest:
        await callback.message.answer(re.sub(r"<[^>]+>", "", text), reply_markup=markup)


@router.callback_query(F.data == "admin_search")
async def admin_search_prompt(callback: CallbackQuery, state: FSMContext):
    if not is_admin(callback.from_user.id):
        return await callback.answer()
    await callback.answer()
    await state.set_state(UserState.waiting_for_admin_search)
    await callback.message.answer(
        "Введи текст для поиска по вопросам или ID пользователя 🔎",
        reply_markup=kb.get_admin_back_keyboard(),
    )


@router.message(UserState.waiting_for_admin_search)
async def admin_search_apply(message: Message, state: FSMContext):
    if not is_admin(message.from_user.id):
        return await state.clear()

    term = (message.text or "").strip()
    await state.set_state(None)

    # Чисто цифровой ввод трактуем как ID пользователя.
    if term.isdigit():
        scope = f"u{term}"
    else:
        await state.update_data(admin_search_term=term)
        scope = "s"

    await state.update_data(admin_scope=scope, admin_page=0)
    text, markup = await _render_feed(scope, 0, state)
    await message.answer(text, reply_markup=markup)


@router.callback_query(F.data == "admin_stats")
async def admin_stats(callback: CallbackQuery):
    if not is_admin(callback.from_user.id):
        return await callback.answer()
    await callback.answer()
    s = await db.get_admin_stats()

    by_type = "\n".join(f"   • {_esc(t)}: {c}" for t, c in s["by_type"]) or "   —"
    top = "\n".join(
        f"   • <code>{uid}</code> — {c} · {_fmt_dt(last)}" for uid, c, last in s["top_users"]
    ) or "   —"

    f = s["funnel"]

    def pct(part: int) -> str:
        """Доля от стартовавших — иначе абсолютные числа ни о чём не говорят."""
        return f"{part / f['started'] * 100:.0f}%" if f["started"] else "—"

    avg = "\n".join(
        f"   • {'⭐' if cur == 'XTR' else cur}: "
        f"{(amount if cur == 'XTR' else amount / 100):.0f} за платёж ({count})"
        for cur, (amount, count) in s["avg_check"].items()
    ) or "   —"

    await callback.message.edit_text(
        f"📊 <b>Статистика Миры</b>\n\n"
        f"🔮 Раскладов всего: <b>{s['spreads']}</b>\n"
        f"📥 Обращений всего: <b>{s['requests']}</b> "
        f"(🚧 отклонено: {s['rejected']} · ⚠️ сбоев: {s['failed']})\n"
        f"📅 Сегодня: <b>{s['today']}</b> · за 7 дней: <b>{s['week']}</b>\n\n"
        f"<b>Воронка:</b>\n"
        f"   1️⃣ Нажали /start: <b>{f['started']}</b>\n"
        f"   2️⃣ Указали дату рождения: <b>{f['profiled']}</b> ({pct(f['profiled'])})\n"
        f"   3️⃣ Сделали расклад: <b>{f['first_spread']}</b> ({pct(f['first_spread'])})\n"
        f"   4️⃣ Заплатили: <b>{f['paying']}</b> ({pct(f['paying'])})\n"
        f"   🚫 Заблокировали бота: <b>{f['blocked']}</b> ({pct(f['blocked'])})\n\n"
        f"<b>Средний чек:</b>\n{avg}\n\n"
        f"<b>По типам:</b>\n{by_type}\n\n"
        f"<b>Самые активные:</b>\n{top}",
        reply_markup=kb.get_admin_back_keyboard(),
    )


# --- АДМИН: ОПЛАТЫ И ВОЗВРАТЫ ---
@router.callback_query(F.data == "admin_revenue")
async def admin_revenue(callback: CallbackQuery):
    if not is_admin(callback.from_user.id):
        return await callback.answer()
    await callback.answer()

    s = await db.get_revenue_stats()
    payments = await db.get_last_payments(limit=10)

    def money(cur: str, amount: int) -> str:
        return f"{amount} ⭐" if cur == "XTR" else f"{amount / 100:.0f} {cur}"

    total = "\n".join(
        f"   • {money(cur, v['amount'])} ({v['count']} оплат)"
        for cur, v in s["by_currency"].items()
    ) or "   — пока нет оплат"
    month = "\n".join(f"   • {money(c, a)}" for c, a in s["month_by_currency"].items()) or "   • —"
    refunded = "\n".join(f"   • {money(c, a)}" for c, a in s["refunded_by_currency"].items())

    if payments:
        rows = "\n".join(
            f"   {'↩️' if p['refunded'] else '✅'} <code>{p['user_id']}</code> · "
            f"{money(p['currency'], p['amount'])} → {p['candles']} 🕯️ · {_fmt_dt(p['created_at'])} "
            f"<i>(#{p['id']})</i>"
            for p in payments
        )
        hint = f"\n\n<i>Возврат:</i> <code>/refund {payments[0]['id']}</code>"
    else:
        rows, hint = "   — пока нет оплат", ""

    await callback.message.edit_text(
        f"💰 <b>Оплаты и доход</b>\n\n"
        f"<b>Всего получено:</b>\n{total}\n\n"
        f"<b>За 30 дней:</b>\n{month}\n"
        + (f"\n<b>Возвращено:</b>\n{refunded}\n" if refunded else "")
        + f"\n🕯️ Продано свечей: <b>{s['candles_sold']}</b>\n"
        f"🔥 Потрачено пользователями: <b>{s['candles_spent']}</b>\n"
        f"👛 Осталось на балансах: <b>{s['candles_in_wallets']}</b>\n\n"
        f"<b>Последние оплаты:</b>\n{rows}{hint}",
        reply_markup=kb.get_admin_back_keyboard(),
    )


@router.message(F.text.startswith("/refund"))
async def cmd_refund(message: Message):
    """Возврат звёзд за платёж. Telegram обязывает поддерживать возвраты."""
    if not is_admin(message.from_user.id):
        return

    parts = (message.text or "").split()
    if len(parts) != 2 or not parts[1].isdigit():
        return await message.answer("Формат: <code>/refund ID_платежа</code>")

    payment = await db.get_payment_by_id(int(parts[1]))
    if not payment:
        return await message.answer("Платёж не найден 🕯️")
    if payment["refunded"]:
        return await message.answer("Этот платёж уже возвращён.")

    # Автовозврат Telegram умеет только для звёзд; деньги провайдера
    # возвращаются в его личном кабинете, бот лишь фиксирует факт.
    if payment["currency"] == "XTR":
        try:
            await bot.refund_star_payment(
                user_id=payment["user_id"], telegram_payment_charge_id=payment["charge_id"]
            )
        except Exception as e:
            logger.error(f"Ошибка возврата {payment['charge_id']}: {e}", exc_info=True)
            return await message.answer(f"❌ Возврат не прошёл: <code>{_esc(str(e))}</code>")
        paid_back = f"{payment['amount']} ⭐"
        note = "Звёзды вернулись на баланс Telegram."
    else:
        paid_back = f"{payment['amount'] / 100:.0f} {payment['currency']}"
        note = "Деньги нужно вернуть вручную в кабинете платёжного провайдера."

    await db.mark_refunded(payment["charge_id"])

    # Свечи снимаем обратно, иначе возврат денег оставит пользователю оплаченный товар.
    taken, balance = await db.try_charge(
        payment["user_id"], payment["candles"], note=f"Возврат платежа #{payment['id']}"
    )
    shortfall = "" if taken else " ⚠️ Свечи уже потрачены — списать не удалось."

    logger.info(f"↩️ [Возврат] {paid_back} пользователю {payment['user_id']}.{shortfall}")
    await message.answer(
        f"↩️ Возвращено <b>{paid_back}</b> пользователю <code>{payment['user_id']}</code>.\n"
        f"🕯️ Списано свечей: {payment['candles'] if taken else 0} (баланс: {balance}){shortfall}\n\n"
        f"<i>{note}</i>"
    )
    try:
        await bot.send_message(
            payment["user_id"],
            f"↩️ Возврат {paid_back} выполнен. {note}",
        )
    except Exception:
        pass


# --- АДМИН: ЦЕНЫ И НАСТРОЙКИ ---
async def _render_settings() -> tuple[str, InlineKeyboardMarkup]:
    prices = db.get_all_prices()
    welcome = db.get_setting_int("welcome_bonus", config.DEFAULT_WELCOME_BONUS)
    referral = db.get_setting_int("referral_bonus", config.DEFAULT_REFERRAL_BONUS)
    provider = "подключён ✅" if _rub_enabled() else "не подключён ❌"
    text = (
        f"⚙️ <b>Цены и настройки</b>\n\n"
        f"Нажми на строку, чтобы изменить значение.\n\n"
        f"💳 Оплата картой: <b>{provider}</b>\n"
        f"<i>Токен провайдера задаётся в .env (PAYMENT_PROVIDER_TOKEN).</i>"
    )
    return text, kb.get_admin_settings_keyboard(prices, welcome, referral)


@router.callback_query(F.data == "admin_settings")
async def admin_settings(callback: CallbackQuery, state: FSMContext):
    if not is_admin(callback.from_user.id):
        return await callback.answer()
    await callback.answer()
    await state.clear()
    text, markup = await _render_settings()
    try:
        await callback.message.edit_text(text, reply_markup=markup)
    except TelegramBadRequest:
        await callback.message.answer(text, reply_markup=markup)


@router.callback_query(F.data.startswith("admin_setprice_"))
async def admin_set_price_prompt(callback: CallbackQuery, state: FSMContext):
    if not is_admin(callback.from_user.id):
        return await callback.answer()
    await callback.answer()
    name = callback.data[len("admin_setprice_"):]
    await state.set_state(UserState.waiting_for_setting_value)
    await state.update_data(setting_key=f"price_{name}", setting_label=f"Цена «{name}»")
    await callback.message.answer(
        f"Введи новую цену для <b>«{_esc(name)}»</b> в свечах (сейчас {db.get_price(name)}):",
        reply_markup=kb.get_admin_back_keyboard(),
    )


@router.callback_query(F.data.in_({"admin_set_welcome_bonus", "admin_set_referral_bonus"}))
async def admin_set_bonus_prompt(callback: CallbackQuery, state: FSMContext):
    if not is_admin(callback.from_user.id):
        return await callback.answer()
    await callback.answer()
    key = callback.data.removeprefix("admin_set_")
    label = "Стартовый бонус" if key == "welcome_bonus" else "Бонус за друга"
    await state.set_state(UserState.waiting_for_setting_value)
    await state.update_data(setting_key=key, setting_label=label)
    await callback.message.answer(
        f"Введи новое значение «{label}» в свечах (сейчас {db.get_setting_int(key)}):",
        reply_markup=kb.get_admin_back_keyboard(),
    )


@router.message(UserState.waiting_for_setting_value)
async def admin_save_setting(message: Message, state: FSMContext):
    if not is_admin(message.from_user.id):
        return await state.clear()

    raw = (message.text or "").strip()
    if not raw.isdigit():
        return await message.answer("Нужно целое число ≥ 0. Попробуй ещё раз:")

    data = await state.get_data()
    key, label = data.get("setting_key"), data.get("setting_label", "Значение")
    await state.set_state(None)
    await db.set_setting(key, int(raw))
    logger.info(f"⚙️ [Админ] {label} -> {raw}")

    await message.answer(f"✅ {label}: <b>{raw}</b> 🕯️")
    text, markup = await _render_settings()
    await message.answer(text, reply_markup=markup)


@router.callback_query(F.data == "admin_packages")
async def admin_packages(callback: CallbackQuery, state: FSMContext):
    if not is_admin(callback.from_user.id):
        return await callback.answer()
    await callback.answer()
    await state.clear()
    packages = await db.get_topup_packages(only_active=False)
    try:
        await callback.message.edit_text(
            "📦 <b>Пакеты пополнения</b>\n\nНажми на пакет, чтобы изменить цену или отключить его.",
            reply_markup=kb.get_admin_packages_keyboard(packages),
        )
    except TelegramBadRequest:
        pass


@router.callback_query(F.data.startswith("admin_pkg_"))
async def admin_package_card(callback: CallbackQuery):
    if not is_admin(callback.from_user.id):
        return await callback.answer()
    await callback.answer()
    pkg = await db.get_topup_package(int(callback.data.rsplit("_", 1)[1]))
    if not pkg:
        return
    await callback.message.edit_text(
        f"📦 <b>{_esc(pkg['title'])}</b>\n\n"
        f"🕯️ Свечей: <b>{pkg['candles']}</b>\n"
        f"⭐ Звёзды: <b>{pkg['stars']}</b>\n"
        f"₽ Рубли: <b>{pkg['rub']}</b>\n"
        f"Статус: <b>{'активен' if pkg['active'] else 'отключён'}</b>",
        reply_markup=kb.get_admin_package_edit_keyboard(pkg),
    )


@router.callback_query(F.data.startswith("admin_pkgtoggle_"))
async def admin_package_toggle(callback: CallbackQuery):
    if not is_admin(callback.from_user.id):
        return await callback.answer()
    pkg = await db.get_topup_package(int(callback.data.rsplit("_", 1)[1]))
    if not pkg:
        return await callback.answer()
    await db.update_package(pkg["id"], active=0 if pkg["active"] else 1)
    await callback.answer("Готово")
    await admin_package_card(callback)


@router.callback_query(F.data.startswith("admin_pkgset_"))
async def admin_package_set_prompt(callback: CallbackQuery, state: FSMContext):
    if not is_admin(callback.from_user.id):
        return await callback.answer()
    await callback.answer()
    field, pkg_id = callback.data[len("admin_pkgset_"):].rsplit("_", 1)
    labels = {"candles": "количество свечей", "stars": "цену в звёздах", "rub": "цену в рублях"}
    await state.set_state(UserState.waiting_for_package_value)
    await state.update_data(pkg_id=int(pkg_id), pkg_field=field)
    await callback.message.answer(
        f"Введи новое значение — {labels.get(field, field)}:",
        reply_markup=kb.get_admin_back_keyboard(),
    )


@router.message(UserState.waiting_for_package_value)
async def admin_package_save(message: Message, state: FSMContext):
    if not is_admin(message.from_user.id):
        return await state.clear()

    raw = (message.text or "").strip()
    if not raw.isdigit() or int(raw) <= 0:
        return await message.answer("Нужно целое число больше нуля. Попробуй ещё раз:")

    data = await state.get_data()
    pkg_id, field = data.get("pkg_id"), data.get("pkg_field")
    await state.set_state(None)
    await db.update_package(pkg_id, **{field: int(raw)})
    logger.info(f"⚙️ [Админ] Пакет #{pkg_id}: {field} -> {raw}")

    pkg = await db.get_topup_package(pkg_id)
    await message.answer(
        f"✅ Обновлено: <b>{pkg['candles']}</b> 🕯️ — {pkg['stars']}⭐ / {pkg['rub']}₽",
        reply_markup=kb.get_admin_package_edit_keyboard(pkg),
    )


@router.message(F.text.startswith("/give"))
async def cmd_give(message: Message):
    """Ручное начисление свечей: акции, компенсации, оплата вне бота."""
    if not is_admin(message.from_user.id):
        return

    parts = (message.text or "").split(maxsplit=3)
    if len(parts) < 3 or not parts[2].lstrip("-").isdigit():
        return await message.answer(
            "Формат: <code>/give ID_или_@username количество [причина]</code>\n"
            "Отрицательное количество — списание.\n"
            "Например: <code>/give 123456 50 компенсация</code>"
        )

    # В спорной ситуации у администратора обычно есть @username, а не числовой
    # ID. Ищем его среди тех, кто уже писал боту.
    target_raw = parts[1]
    if target_raw.isdigit():
        target_id = int(target_raw)
    else:
        target_id = await db.find_user_by_username(target_raw)
        if not target_id:
            return await message.answer(
                f"❌ Не нашла <code>{_esc(target_raw)}</code> среди пользователей.\n"
                f"<i>Username запоминается, когда человек пишет боту. "
                f"Если его нет — начисляй по числовому ID.</i>"
            )

    amount = int(parts[2])
    reason = parts[3] if len(parts) > 3 else "Начисление администратором"

    if amount >= 0:
        balance = await db.add_candles(target_id, amount, kind="admin", note=reason)
        sign = f"+{amount}"
    else:
        taken, balance = await db.try_charge(target_id, -amount, note=reason)
        if not taken:
            return await message.answer(f"❌ Недостаточно свечей: на балансе {balance}.")
        sign = str(amount)

    await message.answer(
        f"✅ <code>{target_id}</code>: <b>{sign}</b> 🕯️ → баланс <b>{balance}</b>"
    )
    try:
        await bot.send_message(
            target_id,
            f"🕯️ Твой баланс изменён: <b>{sign}</b> свечей.\n"
            f"Сейчас на балансе: <b>{balance}</b>.\n<i>{_esc(reason)}</i>",
        )
    except Exception:
        pass


# --- АДМИН: РАССЫЛКА ---
BROADCAST_RATE = 20  # сообщений в секунду; лимит Telegram ~30, берём с запасом


@router.callback_query(F.data == "admin_broadcast")
async def admin_broadcast_prompt(callback: CallbackQuery, state: FSMContext):
    if not is_admin(callback.from_user.id):
        return await callback.answer()
    await callback.answer()
    await state.set_state(UserState.waiting_for_broadcast_text)
    recipients = len(await db.get_all_user_ids())
    await callback.message.answer(
        f"📢 Напиши текст рассылки.\n\n"
        f"Получателей: <b>{recipients}</b>\n"
        f"Можно использовать <b>жирный</b>, <i>курсив</i> и другие HTML-теги.",
        reply_markup=kb.get_admin_back_keyboard(),
    )


@router.message(UserState.waiting_for_broadcast_text)
async def admin_broadcast_preview(message: Message, state: FSMContext):
    """Показывает текст ровно так, как его увидят пользователи, и ждёт подтверждения."""
    if not is_admin(message.from_user.id):
        return await state.clear()

    text = message.html_text if message.text else ""
    if not text.strip():
        return await message.answer("Пустой текст. Напиши сообщение для рассылки 🕯️")

    await state.set_state(None)
    await state.update_data(broadcast_text=text)
    recipients = len(await db.get_all_user_ids())

    try:
        await message.answer(text)
    except TelegramBadRequest as e:
        return await message.answer(
            f"❌ Ошибка разметки: <code>{_esc(str(e))}</code>\nИсправь текст и пришли снова.",
            reply_markup=kb.get_admin_back_keyboard(),
        )

    await message.answer(
        f"👆 Так увидят сообщение пользователи.\n\nОтправить <b>{recipients}</b> получателям?",
        reply_markup=kb.get_broadcast_confirm_keyboard(),
    )


@router.callback_query(F.data == "admin_bc_send")
async def admin_broadcast_send(callback: CallbackQuery, state: FSMContext):
    if not is_admin(callback.from_user.id):
        return await callback.answer()
    await callback.answer()

    text = (await state.get_data()).get("broadcast_text")
    if not text:
        return await callback.message.answer(
            "Текст рассылки потерян. Начни заново 🕯️", reply_markup=kb.get_admin_back_keyboard()
        )

    user_ids = await db.get_all_user_ids()
    status = await callback.message.answer(f"📤 Рассылка запущена: 0 / {len(user_ids)}...")

    sent = blocked = failed = 0
    for i, uid in enumerate(user_ids, 1):
        try:
            await bot.send_message(uid, text)
            sent += 1
        except TelegramRetryAfter as e:
            # Флуд-контроль Telegram: ждём ровно столько, сколько просят, и повторяем.
            logger.warning(f"⏳ [Рассылка] Флуд-контроль, пауза {e.retry_after} сек.")
            await asyncio.sleep(e.retry_after)
            try:
                await bot.send_message(uid, text)
                sent += 1
            except Exception:
                failed += 1
        except TelegramForbiddenError:
            # Пользователь заблокировал бота или удалил аккаунт. Запоминаем:
            # иначе каждая следующая рассылка снова стучится в закрытую дверь.
            blocked += 1
            await db.mark_blocked(uid)
        except Exception as e:
            failed += 1
            logger.warning(f"⚠️ [Рассылка] Ошибка для {uid}: {e}")

        if i % 25 == 0:
            try:
                await status.edit_text(f"📤 Рассылка: {i} / {len(user_ids)}...")
            except TelegramBadRequest:
                pass
        await asyncio.sleep(1 / BROADCAST_RATE)

    await state.clear()
    logger.info(f"📢 [Рассылка] Готово: {sent} доставлено, {blocked} заблокировали, {failed} ошибок.")
    await status.edit_text(
        f"📢 <b>Рассылка завершена</b>\n\n"
        f"✅ Доставлено: <b>{sent}</b>\n"
        f"🚫 Заблокировали бота: <b>{blocked}</b>\n"
        f"⚠️ Ошибок: <b>{failed}</b>",
        reply_markup=kb.get_admin_back_keyboard(),
    )


# --- АДМИН: БЭКАП БАЗЫ ---
@router.callback_query(F.data == "admin_backup")
async def admin_backup(callback: CallbackQuery):
    if not is_admin(callback.from_user.id):
        return await callback.answer()
    await callback.answer()

    status = await callback.message.answer("💾 Готовлю выгрузку пользовательских данных...")
    # Тот же каталог, что и у ежедневного бэкапа: на сервере это том с данными,
    # а не рабочая папка контейнера.
    os.makedirs(config.BACKUP_DIR, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d_%H%M")
    path = os.path.join(config.BACKUP_DIR, f"backup_mira_{stamp}.db")
    keep_file = False

    try:
        counts = await asyncio.to_thread(db.create_backup_sync, path, False)
        size_mb = counts.pop("_bytes") / 1024 / 1024

        if size_mb > 45:
            # Файл остаётся на диске — иначе сообщение ниже врало бы: в finally
            # он удалялся сразу после того, как админу сказали, где его искать.
            keep_file = True
            await status.edit_text(
                f"⚠️ Бэкап получился {size_mb:.1f} МБ — Telegram не примет файл больше 50 МБ.\n"
                f"Файл сохранён на сервере: <code>{_esc(path)}</code>",
                reply_markup=kb.get_admin_back_keyboard(),
            )
            return

        rows = "\n".join(f"   • {t}: {c}" for t, c in counts.items())
        await callback.message.answer_document(
            FSInputFile(path),
            caption=(
                f"💾 <b>Бэкап данных</b> (профили, расклады, балансы, платежи)\n"
                f"{datetime.now().strftime('%d.%m.%Y %H:%M')} · {size_mb:.2f} МБ\n\n"
                f"{rows}\n\n"
                f"<i>База знаний (~190 МБ) не включена — она восстанавливается из CSV.</i>"
            ),
        )
        await status.delete()
    except Exception as e:
        logger.error(f"Ошибка бэкапа: {e}", exc_info=True)
        await status.edit_text(
            f"❌ Не удалось создать бэкап: <code>{_esc(str(e))}</code>",
            reply_markup=kb.get_admin_back_keyboard(),
        )
    finally:
        # Файл уже отправлен в Telegram — на диске он не нужен. Исключение:
        # слишком большой бэкап, который забрать можно только с сервера.
        if not keep_file:
            try:
                if os.path.exists(path):
                    os.remove(path)
            except OSError:
                pass
