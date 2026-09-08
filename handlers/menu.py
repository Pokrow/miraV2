"""Меню и навигация: старт, справка, профиль, дневник, инлайн-кнопки."""

import logging
import math

from aiogram import F, Router
from aiogram.exceptions import TelegramBadRequest
from aiogram.filters import Command, CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, InlineKeyboardMarkup, Message

import config
import database as db
import keyboards as kb
from bot_core import UserState
from utils import _esc, answer_long, delete_prompt_msg

logger = logging.getLogger("mira_bot")
router = Router(name="menu")


# --- ИНЛАЙН КНОПКИ ---
@router.callback_query(F.data == "cancel_fsm_action")
async def process_inline_cancel(callback: CallbackQuery, state: FSMContext):
    await state.clear()
    try:
        await callback.message.delete()
    except Exception:
        pass
    await callback.message.answer("Действие отменено. Выбери нужный раздел в меню ниже 👇",
                                  reply_markup=kb.get_main_keyboard())


@router.callback_query(F.data.startswith("select_spread_"))
async def start_inline_spread(callback: CallbackQuery, state: FSMContext):
    await callback.answer()
    spread_map = {
        "select_spread_3": ("🔮 3 Карты",
                            "Расклад на три состояния. Сосредоточься на ситуации и сформулируй свой вопрос... 🤍",
                            {"type": "3 Карты", "count": 3, "fast": False}),
        "select_spread_love": ("❤️ Отношения",
                               "Расклад «Отношения» (5 карт): <b>Ты / Партнер / Связь / Препятствие / Совет</b>. Напиши свой вопрос о чувствах или союзе... 🤍",
                               {"type": "Отношения", "count": 5, "fast": False}),
        "select_spread_yesno": ("⚖️ Да / Нет", "Задай вопрос, требующий ясного ответа «Да» или «Нет» ⚖️",
                                {"type": "Да / Нет", "count": 1, "fast": True}),
        "select_spread_compat": ("💞 Совместимость",
                                 "Напиши дату рождения партнера в формате ДД.ММ.ГГГГ (например, 12.04.1995) 🕯️", None),
    }
    cfg = spread_map.get(callback.data)
    if not cfg: return

    if callback.data == "select_spread_compat":
        if not await db.get_user_birth_date(callback.from_user.id):
            return await callback.message.answer(
                "Сначала укажи свою дату рождения 🕯️ Без неё не рассчитать совместный Аркан.\n"
                "Нажми «⚙️ Изменить дату рождения» в профиле.")
        prompt_msg = await callback.message.answer(cfg[1], reply_markup=kb.get_cancel_inline_keyboard())
        await state.set_state(UserState.waiting_for_partner_birthdate)
    else:
        await state.update_data(spread_config=cfg[2])
        prompt_msg = await callback.message.answer(cfg[1], reply_markup=kb.get_cancel_inline_keyboard())
        await state.set_state(UserState.waiting_for_tarot_question)
    await state.update_data(prompt_msg_id=prompt_msg.message_id)


@router.callback_query(F.data == "change_birthdate")
async def inline_change_date(callback: CallbackQuery, state: FSMContext):
    await callback.answer()
    prompt_msg = await callback.message.answer(
        "Укажи новую дату рождения в формате ДД.ММ.ГГГГ (например, 17.05.1990) 🕯️",
        reply_markup=kb.get_cancel_inline_keyboard())
    await state.set_state(UserState.waiting_for_birthdate)
    await state.update_data(prompt_msg_id=prompt_msg.message_id)


@router.callback_query(F.data == "open_spreads")
async def open_spreads(callback: CallbackQuery, state: FSMContext):
    await callback.answer()
    await state.clear()
    await callback.message.answer(
        "Выбери формат расклада, который лучше всего откликается твоему запросу 👇",
        reply_markup=kb.get_spreads_inline_keyboard())


# --- ОБРАЩЕНИЕ К КЛИЕНТУ ---
@router.callback_query(F.data == "change_gender")
async def change_gender(callback: CallbackQuery):
    await callback.answer()
    await callback.message.answer(
        "Как мне обращаться к тебе в раскладах? 🕯️\n"
        "<i>Это влияет только на род в моих словах — «ты пришёл» или «ты пришла».</i>",
        reply_markup=kb.get_gender_inline_keyboard())


@router.callback_query(F.data.startswith("set_gender_"))
async def save_gender(callback: CallbackQuery):
    gender = callback.data.rsplit("_", 1)[1]
    await db.set_user_gender(callback.from_user.id, gender)
    await callback.answer("Запомнила 🤍")
    replies = {
        "m": "Хорошо, буду говорить с тобой в мужском роде 🕯️",
        "f": "Хорошо, буду говорить с тобой в женском роде 🕯️",
        "x": "Хорошо, буду обращаться нейтрально 🕯️",
    }
    try:
        await callback.message.edit_text(replies.get(gender, replies["x"]))
    except TelegramBadRequest:
        await callback.message.answer(replies.get(gender, replies["x"]))


@router.callback_query(F.data.startswith("view_history_"))
async def view_history_item(callback: CallbackQuery):
    await callback.answer()
    spread_id = int(callback.data.replace("view_history_", ""))
    record = await db.get_spread_by_id(spread_id)
    if not record: return await callback.message.answer("Расклад не найден 🕯️")
    # Вопрос экранируется: он хранится сырым, и «почему <он> молчит» ломало
    # разметку так, что запись дневника не открывалась вообще.
    text = (f"📜 <b>{_esc(record['spread_type'])}</b>\n📅 <i>{record['created_at'][:16]}</i>\n"
            f"❓ <b>Вопрос:</b> {_esc(record['question'])}\n\n"
            f"🃏 <b>Карты:</b>\n{_esc(record['cards_text'])}\n\n{record['answer_text']}")
    # Уточнение — одно на расклад: если оно уже было, кнопку не показываем.
    if await db.clarification_available(spread_id):
        markup = kb.get_clarify_inline_keyboard(spread_id)
    else:
        text += "\n\n<i>💬 Уточнение по этому раскладу уже было.</i>"
        markup = kb.get_after_spread_keyboard()
    # Вопрос, карты и полный ответ в одном сообщении легко перебирают лимит.
    await answer_long(callback.message, text, reply_markup=markup)


@router.callback_query(F.data.startswith("clarify_spread_"))
async def start_specific_clarification(callback: CallbackQuery, state: FSMContext):
    await callback.answer()
    spread_id = int(callback.data.replace("clarify_spread_", ""))
    if not await db.clarification_available(spread_id):
        return await callback.message.answer(
            "По этому раскладу уточнение уже было 🕯️\n"
            "<i>Одно бесплатное уточнение на расклад. Если тема не отпускает — сделай новый расклад,"
            " карты ответят с другой стороны.</i>",
            reply_markup=kb.get_after_spread_keyboard())

    await state.set_state(UserState.waiting_for_clarification)
    await state.update_data(target_spread_id=spread_id)
    prompt_msg = await callback.message.answer(
        "Что именно в этом раскладе ты хочешь разобрать подробнее? 🕯️\n"
        "<i>Это уточнение бесплатное, и оно одно на расклад — спроси о самом важном.</i>",
        reply_markup=kb.get_cancel_inline_keyboard())
    await state.update_data(prompt_msg_id=prompt_msg.message_id)


# --- ОСНОВНЫЕ ХЕНДЛЕРЫ МЕНЮ ---
@router.message(CommandStart())
async def cmd_start(message: Message, state: FSMContext):
    await delete_prompt_msg(message, state)
    await state.clear()
    user_id = message.from_user.id
    logger.info(f"👤 [User {user_id}] Запустил /start")

    # Человек вернулся — значит, бот у него разблокирован. Снимаем отметку,
    # иначе он навсегда выпал бы из рассылок и напоминаний.
    await db.mark_blocked(user_id, False)

    # Реферальная ссылка вида /start ref123456. Связь фиксируется до создания
    # профиля: register_referral не засчитает уже знакомого боту пользователя.
    payload = (message.text or "").partition(" ")[2].strip()
    if payload.startswith("ref"):
        inviter_raw = payload[3:]
        if inviter_raw.isdigit():
            if await db.register_referral(user_id, int(inviter_raw)):
                logger.info(f"🎁 [Реферал] {user_id} пришёл от {inviter_raw}.")

    # Стартовые свечи выдаются один раз — чтобы человек попробовал бота до оплаты.
    bonus = await db.grant_welcome_bonus(user_id)
    if bonus:
        await message.answer(
            f"🎁 Дарю тебе <b>{bonus}</b> свечей на первые расклады 🕯️\n"
            f"<i>Каждый вопрос к картам стоит несколько свечей — их всегда можно пополнить.</i>"
        )

    arcana = await db.get_user_arcana(user_id)
    if arcana:
        balance = await db.get_balance(user_id)
        await message.answer(
            f"Привет 🤍 Рада снова видеть тебя.\nТвой Аркан по праву рождения — <b>{arcana}</b>.\n"
            f"Баланс: <b>{balance}</b> 🕯️\n\nО чем хочешь спросить карты сегодня?",
            reply_markup=kb.get_main_keyboard())
    else:
        # Анкета необязательна: раньше дата рождения стояла глухой стеной перед
        # первым раскладом — самый дорогой шаг воронки до того, как человек
        # вообще увидел, чем бот полезен. Аркан рождения лишь уточняет трактовку.
        prompt_msg = await message.answer(
            "Привет 🤍 Я Мира.\n\nЧтобы точнее чувствовать тебя, напиши свою дату рождения "
            "в формате ДД.ММ.ГГГГ (например, 17.05.1990) — я рассчитаю твой Аркан по праву рождения ✨\n\n"
            "<i>Можно и пропустить: расклады работают и без неё, а дату всегда получится "
            "указать позже в профиле.</i>",
            reply_markup=kb.get_skip_birthdate_keyboard())
        await state.set_state(UserState.waiting_for_birthdate)
        await state.update_data(prompt_msg_id=prompt_msg.message_id)


@router.callback_query(F.data == "skip_birthdate")
async def skip_birthdate(callback: CallbackQuery, state: FSMContext):
    await callback.answer()
    await state.clear()
    try:
        await callback.message.delete()
    except TelegramBadRequest:
        pass
    await callback.message.answer(
        "Хорошо, обойдёмся без даты 🕯️ Выбирай, с чего начнём — "
        "первая «Карта дня» сегодня бесплатна.\n\n"
        "<i>Дату рождения можно указать когда захочешь: «🌌 Мой профиль».</i>",
        reply_markup=kb.get_main_keyboard())


@router.message(Command("help"))
async def cmd_help(message: Message, state: FSMContext):
    await state.clear()
    prices = db.get_all_prices()
    price_lines = "\n".join(f"   • {name} — <b>{p}</b> 🕯️" for name, p in prices.items())
    await message.answer(
        f"🕯️ <b>Как всё устроено</b>\n\n"
        f"Я Мира — таро-терапевт. Ты приходишь с вопросом о себе, своих чувствах или выборе, "
        f"а я раскладываю карты и разбираю, что за ними стоит.\n\n"
        f"<b>Разделы меню:</b>\n"
        f"   🃏 <b>Карта дня</b> — короткий совет на сегодня\n"
        f"   🔮 <b>Расклады</b> — 3 карты, отношения, «да / нет», совместимость\n"
        f"   🌌 <b>Мой профиль</b> — арканы, дата рождения, обращение, напоминания\n"
        f"   📜 <b>Мой дневник</b> — все прошлые расклады, можно перечитать\n"
        f"   🕯️ <b>Баланс</b> — свечи, пополнение, история списаний\n\n"
        f"<b>Свечи</b> — внутренняя валюта, один вопрос к картам:\n{price_lines}\n\n"
        f"<b>Бесплатно:</b>\n"
        f"   • первая «Карта дня» каждый день\n"
        f"   • одно уточнение к каждому раскладу\n"
        f"   • свечи за друга, пришедшего по твоей ссылке\n\n"
        f"🔔 В профиле можно включить напоминание о бесплатной карте дня — "
        f"без него я не пишу первой.\n\n"
        + (f"✉️ Вопрос по оплате или что-то пошло не так: {config.SUPPORT_CONTACT}\n\n"
           if config.SUPPORT_CONTACT else "")
        + f"<i>Команды: /start — начать заново, /help — эта справка.</i>",
        reply_markup=kb.get_main_keyboard())


@router.message(F.text == "🔮 Расклады")
async def hub_spreads(message: Message, state: FSMContext):
    await delete_prompt_msg(message, state)
    await state.clear()
    await message.answer("Выбери формат расклада, который лучше всего откликается твоему запросу 👇",
                         reply_markup=kb.get_spreads_inline_keyboard())


@router.message(F.text == "🌌 Мой профиль")
async def hub_profile(message: Message, state: FSMContext):
    await delete_prompt_msg(message, state)
    await state.clear()
    user_id = message.from_user.id
    bdate = await db.get_user_birth_date(user_id)
    arcana = await db.get_user_arcana(user_id)
    reminder_on = await db.get_reminder_enabled(user_id)
    if arcana and bdate:
        year_arcana = db.calculate_personal_year_arcana(bdate)
        gender = config.GENDER_TITLES.get(await db.get_user_gender(user_id), config.GENDER_TITLES["x"])
        await message.answer(
            f"🌌 <b>Твой сакральный профиль:</b>\n\n✨ <b>Аркан по праву рождения:</b> <b>{arcana}</b>\n<i>(Твоя глубинная суть, характер и внутренняя опора души)</i>\n\n🔮 <b>Аркан личного года ({config.CURRENT_YEAR}):</b> <b>{year_arcana}</b>\n<i>(Главный урок, вектор роста и ведущая энергия текущего года)</i>\n\n🧭 <b>Обращение:</b> {gender}",
            reply_markup=kb.get_profile_inline_keyboard(reminder_on))
    else:
        await message.answer("Я ещё не знаю твоей даты рождения. Нажми «⚙️ Изменить дату рождения» ниже 🕯️",
                             reply_markup=kb.get_profile_inline_keyboard(reminder_on))


@router.callback_query(F.data == "toggle_reminder")
async def toggle_reminder(callback: CallbackQuery):
    """Напоминание строго по согласию: включается и выключается самим человеком."""
    user_id = callback.from_user.id
    enabled = not await db.get_reminder_enabled(user_id)
    await db.set_reminder_enabled(user_id, enabled)
    await callback.answer("Напоминания включены 🔔" if enabled else "Напоминания выключены 🔕")
    try:
        await callback.message.edit_reply_markup(
            reply_markup=kb.get_profile_inline_keyboard(enabled)
        )
    except TelegramBadRequest:
        pass


async def _render_diary(user_id: int, page: int) -> tuple[str, InlineKeyboardMarkup | None]:
    """Страница дневника. None вместо клавиатуры — раскладов ещё не было."""
    total = await db.count_user_spreads(user_id)
    if not total:
        return "В твоем дневнике пока пусто 🕯️\nСделай свой первый расклад в меню!", None

    total_pages = max(1, math.ceil(total / db.DIARY_PAGE_SIZE))
    page = max(0, min(page, total_pages - 1))
    items = await db.get_user_spreads_history(
        user_id, limit=db.DIARY_PAGE_SIZE, offset=page * db.DIARY_PAGE_SIZE
    )
    text = (
        f"📜 <b>Твой дневник раскладов</b>\n"
        f"<i>Всего: {total}</i>\n\n"
        f"Нажми на любой расклад ниже, чтобы перечитать его или задать уточняющий вопрос 🕯️"
    )
    return text, kb.get_diary_keyboard(items, page, total_pages)


@router.message(F.text == "📜 Мой дневник")
async def show_history(message: Message, state: FSMContext):
    await delete_prompt_msg(message, state)
    await state.clear()
    text, markup = await _render_diary(message.from_user.id, 0)
    await message.answer(text, reply_markup=markup or kb.get_main_keyboard())


@router.callback_query(F.data.startswith("diary_page_"))
async def diary_page(callback: CallbackQuery):
    await callback.answer()
    page = int(callback.data.rsplit("_", 1)[1])
    text, markup = await _render_diary(callback.from_user.id, page)
    try:
        await callback.message.edit_text(text, reply_markup=markup)
    except TelegramBadRequest:
        await callback.message.answer(text, reply_markup=markup)


@router.callback_query(F.data == "diary_noop")
async def diary_noop(callback: CallbackQuery):
    await callback.answer()


@router.message(F.text == "🃏 Карта дня")
async def ask_card_of_day(message: Message, state: FSMContext):
    await delete_prompt_msg(message, state)
    await state.clear()

    # Первая карта за сутки — подарок: это ежедневный повод вернуться к картам.
    is_free = await db.free_card_available(message.from_user.id)
    price = db.get_price("Карта дня")
    cost_line = (
        "<i>🎁 Сегодняшняя карта — бесплатно.</i>"
        if is_free
        else f"<i>Бесплатная карта на сегодня уже открыта. Ещё одна — {price} свечей.</i>"
    )

    await state.update_data(spread_config={"type": "Карта дня", "count": 1, "fast": False})
    prompt_msg = await message.answer(
        f"Сделай глубокий вдох... Напиши свой вопрос или просто скажи <b>«тяни»</b>, "
        f"если хочешь получить совет на день 🕯️\n\n{cost_line}",
        reply_markup=kb.get_cancel_inline_keyboard())
    await state.set_state(UserState.waiting_for_tarot_question)
    await state.update_data(prompt_msg_id=prompt_msg.message_id)
