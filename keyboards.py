from aiogram.utils.keyboard import InlineKeyboardBuilder, ReplyKeyboardBuilder
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

import config

def get_main_keyboard():
    builder = ReplyKeyboardBuilder()
    builder.button(text="🃏 Карта дня")
    builder.button(text="🔮 Расклады")
    builder.button(text="🌌 Мой профиль")
    builder.button(text="📜 Мой дневник")
    builder.button(text="🕯️ Баланс")
    builder.adjust(2, 2, 1)
    return builder.as_markup(resize_keyboard=True)

def get_spreads_inline_keyboard() -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    builder.button(text="🔮 3 Карты", callback_data="select_spread_3")
    builder.button(text="❤️ Отношения (5 карт)", callback_data="select_spread_love")
    builder.button(text="⚖️ Да / Нет (Быстрый ответ)", callback_data="select_spread_yesno")
    builder.button(text="💞 Совместимость по Арканам", callback_data="select_spread_compat")
    builder.adjust(1)
    return builder.as_markup()

def get_profile_inline_keyboard(reminder_on: bool = False) -> InlineKeyboardMarkup:
    """Профиль — только данные о клиенте. Совместимость живёт в разделе раскладов:
    это платный расклад, а не свойство профиля."""
    builder = InlineKeyboardBuilder()
    builder.button(text="⚙️ Изменить дату рождения", callback_data="change_birthdate")
    builder.button(text="🧭 Обращение ко мне", callback_data="change_gender")
    builder.button(
        text=f"🔔 Напоминать о карте дня: {'вкл' if reminder_on else 'выкл'}",
        callback_data="toggle_reminder",
    )
    builder.adjust(1)
    return builder.as_markup()


def get_gender_inline_keyboard() -> InlineKeyboardMarkup:
    """Без явного выбора модель на русском по умолчанию говорит в женском роде.

    Предлагаются только мужской и женский род. Нейтральное обращение осталось
    поведением по умолчанию — для тех, кто ничего не выбрал, — но отдельной
    кнопкой не навязывается.
    """
    builder = InlineKeyboardBuilder()
    builder.button(text="♂️ Мужской род", callback_data="set_gender_m")
    builder.button(text="♀️ Женский род", callback_data="set_gender_f")
    builder.adjust(2)
    return builder.as_markup()

def get_skip_birthdate_keyboard() -> InlineKeyboardMarkup:
    """Анкета на входе необязательна — иначе часть людей уходит, не начав."""
    builder = InlineKeyboardBuilder()
    builder.button(text="⏭ Пропустить", callback_data="skip_birthdate")
    return builder.as_markup()


def get_cancel_inline_keyboard() -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    builder.button(text="❌ Отмена", callback_data="cancel_fsm_action")
    return builder.as_markup()

def get_clarify_inline_keyboard(spread_id: int) -> InlineKeyboardMarkup:
    """Кнопка уточнения. Уточнение бесплатное и одно на расклад — это сказано
    прямо на кнопке, чтобы человек не искал списание в балансе.

    Расклад всегда сохранён к моменту показа кнопки, поэтому spread_id
    обязателен: прежний запасной вариант вёл на callback без обработчика.
    """
    builder = InlineKeyboardBuilder()
    builder.button(text="💬 Уточнить по раскладу (бесплатно)",
                   callback_data=f"clarify_spread_{spread_id}")
    return builder.as_markup()


def get_diary_keyboard(items: list[dict], page: int, total_pages: int) -> InlineKeyboardMarkup:
    """Страница дневника: расклад на строку плюс навигация, если страниц больше одной."""
    builder = InlineKeyboardBuilder()
    for item in items:
        builder.button(text=f"🔮 {item['spread_type']} ({item['created_at'][:10]})",
                       callback_data=f"view_history_{item['id']}")
    builder.adjust(1)

    if total_pages > 1:
        nav = []
        if page > 0:
            nav.append(InlineKeyboardButton(text="⬅️", callback_data=f"diary_page_{page - 1}"))
        nav.append(InlineKeyboardButton(text=f"{page + 1}/{total_pages}", callback_data="diary_noop"))
        if page < total_pages - 1:
            nav.append(InlineKeyboardButton(text="➡️", callback_data=f"diary_page_{page + 1}"))
        builder.row(*nav)
    return builder.as_markup()


def get_after_spread_keyboard() -> InlineKeyboardMarkup:
    """Показывается, когда уточнение по раскладу уже израсходовано."""
    builder = InlineKeyboardBuilder()
    builder.button(text="🔮 Новый расклад", callback_data="open_spreads")
    return builder.as_markup()

def get_topup_keyboard(packages: list[dict], show_rub: bool) -> InlineKeyboardMarkup:
    """Пакеты пополнения. Кнопка оплаты рублями появляется только если
    провайдер подключён — иначе счёт всё равно не выставится."""
    builder = InlineKeyboardBuilder()
    for p in packages:
        builder.button(
            text=f"🕯️ {p['candles']} — {p['stars']} ⭐",
            callback_data=f"pay_stars_{p['id']}",
        )
        if show_rub:
            builder.button(
                text=f"🕯️ {p['candles']} — {p['rub']} ₽",
                callback_data=f"pay_rub_{p['id']}",
            )
    builder.button(text="🎁 Пригласить друга", callback_data="referral_info")
    builder.adjust(2 if show_rub else 1)
    return builder.as_markup()


def _support_button(builder: InlineKeyboardBuilder) -> None:
    """Кнопка поддержки. Без контакта в конфиге не показывается вовсе —
    мёртвая кнопка хуже её отсутствия."""
    contact = config.SUPPORT_CONTACT
    if not contact:
        return
    if contact.startswith("http"):
        builder.button(text="✉️ Написать в поддержку", url=contact)
    else:
        builder.button(text="✉️ Написать в поддержку", url=f"https://t.me/{contact.lstrip('@')}")


def get_low_balance_keyboard() -> InlineKeyboardMarkup:
    """Показывается, когда свечей не хватает на запрос."""
    builder = InlineKeyboardBuilder()
    builder.button(text="🕯️ Пополнить баланс", callback_data="show_topup")
    builder.button(text="🎁 Пригласить друга — свечи в подарок", callback_data="referral_info")
    _support_button(builder)
    builder.adjust(1)
    return builder.as_markup()


def get_balance_keyboard() -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    builder.button(text="🕯️ Пополнить баланс", callback_data="show_topup")
    builder.button(text="📖 История списаний", callback_data="show_history_tx")
    builder.button(text="🎁 Пригласить друга", callback_data="referral_info")
    _support_button(builder)
    builder.adjust(1)
    return builder.as_markup()


def get_referral_keyboard(link: str) -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    builder.button(
        text="📤 Поделиться ссылкой",
        url=f"https://t.me/share/url?url={link}&text=Мира%20—%20таро-терапевт.%20Приходи%20за%20раскладом%20🕯️",
    )
    builder.button(text="🕯️ Пополнить баланс", callback_data="show_topup")
    builder.adjust(1)
    return builder.as_markup()


def get_admin_keyboard() -> InlineKeyboardMarkup:
    """Главное меню админ-панели."""
    builder = InlineKeyboardBuilder()
    builder.button(text="📋 Запросы пользователей", callback_data="admin_feed_0")
    builder.button(text="🔎 Поиск по запросам", callback_data="admin_search")
    builder.button(text="📊 Статистика", callback_data="admin_stats")
    builder.button(text="💰 Оплаты и доход", callback_data="admin_revenue")
    builder.button(text="⚙️ Цены и настройки", callback_data="admin_settings")
    builder.button(text="📢 Рассылка", callback_data="admin_broadcast")
    builder.button(text="💾 Скачать БД", callback_data="admin_backup")
    builder.adjust(1)
    return builder.as_markup()


def get_broadcast_confirm_keyboard() -> InlineKeyboardMarkup:
    """Подтверждение рассылки: отправка необратима, поэтому только явным нажатием."""
    builder = InlineKeyboardBuilder()
    builder.button(text="✅ Отправить всем", callback_data="admin_bc_send")
    builder.button(text="✏️ Изменить текст", callback_data="admin_broadcast")
    builder.button(text="❌ Отмена", callback_data="admin_home")
    builder.adjust(1)
    return builder.as_markup()


def get_admin_feed_keyboard(items: list[dict], page: int, total_pages: int,
                            scope: str = "all") -> InlineKeyboardMarkup:
    """Лента запросов: кнопка на каждый запрос + постраничная навигация.

    scope кодирует активный фильтр: "all", "u<user_id>" или "s<строка поиска>".
    """
    builder = InlineKeyboardBuilder()

    for it in items:
        q = (it["question"] or "").replace("\n", " ")
        if len(q) > 30:
            q = q[:30] + "…"
        # Отклонённые и сорвавшиеся запросы важно отличать в списке: именно их
        # раньше не было видно вовсе.
        mark = {"rejected": "🚧", "error": "⚠️"}.get(it.get("status", "ok"), "🔍")
        builder.button(text=f"{mark} #{it['id']} · {q}", callback_data=f"admin_view_{it['id']}")
    builder.adjust(1)

    nav = []
    if page > 0:
        nav.append(("⬅️", f"admin_page_{scope}_{page - 1}"))
    if total_pages > 1:
        nav.append((f"{page + 1}/{total_pages}", "admin_noop"))
    if page < total_pages - 1:
        nav.append(("➡️", f"admin_page_{scope}_{page + 1}"))
    if nav:
        for text, cb in nav:
            builder.button(text=text, callback_data=cb)
        builder.adjust(*([1] * len(items)), len(nav))

    builder.row(InlineKeyboardButton(text="⬅️ В админ-меню", callback_data="admin_home"))
    return builder.as_markup()


def get_admin_entry_keyboard(user_id: int, scope: str, page: int) -> InlineKeyboardMarkup:
    """Кнопки под конкретным запросом: перейти к автору или вернуться в ленту."""
    builder = InlineKeyboardBuilder()
    builder.button(text=f"👤 Все запросы {user_id}", callback_data=f"admin_page_u{user_id}_0")
    builder.button(text="⬅️ К списку", callback_data=f"admin_page_{scope}_{page}")
    builder.button(text="🏠 В админ-меню", callback_data="admin_home")
    builder.adjust(1)
    return builder.as_markup()


def get_admin_back_keyboard() -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    builder.button(text="⬅️ В админ-меню", callback_data="admin_home")
    return builder.as_markup()


def get_admin_settings_keyboard(prices: dict, welcome: int, referral: int) -> InlineKeyboardMarkup:
    """Настройки: тап по строке открывает ввод нового значения."""
    builder = InlineKeyboardBuilder()
    for name, price in prices.items():
        builder.button(text=f"💵 {name}: {price} 🕯️", callback_data=f"admin_setprice_{name}")
    builder.button(text=f"🎁 Стартовый бонус: {welcome} 🕯️", callback_data="admin_set_welcome_bonus")
    builder.button(text=f"👥 За друга: {referral} 🕯️", callback_data="admin_set_referral_bonus")
    builder.button(text="📦 Пакеты пополнения", callback_data="admin_packages")
    builder.button(text="⬅️ В админ-меню", callback_data="admin_home")
    builder.adjust(1)
    return builder.as_markup()


def get_admin_packages_keyboard(packages: list[dict]) -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    for p in packages:
        mark = "" if p["active"] else "🚫 "
        builder.button(
            text=f"{mark}{p['candles']} 🕯️ — {p['stars']}⭐ / {p['rub']}₽",
            callback_data=f"admin_pkg_{p['id']}",
        )
    builder.button(text="⬅️ К настройкам", callback_data="admin_settings")
    builder.adjust(1)
    return builder.as_markup()


def get_admin_package_edit_keyboard(pkg: dict) -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    builder.button(text=f"🕯️ Свечей: {pkg['candles']}", callback_data=f"admin_pkgset_candles_{pkg['id']}")
    builder.button(text=f"⭐ Цена в звёздах: {pkg['stars']}", callback_data=f"admin_pkgset_stars_{pkg['id']}")
    builder.button(text=f"₽ Цена в рублях: {pkg['rub']}", callback_data=f"admin_pkgset_rub_{pkg['id']}")
    builder.button(
        text="🚫 Отключить" if pkg["active"] else "✅ Включить",
        callback_data=f"admin_pkgtoggle_{pkg['id']}",
    )
    builder.button(text="⬅️ К пакетам", callback_data="admin_packages")
    builder.adjust(1)
    return builder.as_markup()