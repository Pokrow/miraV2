"""Ядро бота: логирование, сессия, экземпляры Bot и Dispatcher, состояния FSM.

Вынесено отдельно, чтобы роутеры могли обращаться к `bot` и `UserState`,
не импортируя main.py и не создавая кольцевых импортов.
"""

import logging
import socket
import time
from logging.handlers import RotatingFileHandler

import aiohttp
from aiogram import BaseMiddleware, Bot, Dispatcher
from aiogram.client.default import DefaultBotProperties
from aiogram.client.session.aiohttp import AiohttpSession
from aiogram.enums import ParseMode
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import CallbackQuery, Message

import config

try:
    from aiohttp_socks import ProxyConnector
except ImportError:
    ProxyConnector = None

# --- НАСТРОЙКА ЛОГИРОВАНИЯ ---
logger = logging.getLogger("mira_bot")
if not logger.handlers:
    logger.setLevel(logging.INFO)
    formatter = logging.Formatter("%(asctime)s [%(levelname)s] %(message)s", datefmt="%H:%M:%S")

    console_handler = logging.StreamHandler()
    console_handler.setFormatter(formatter)
    logger.addHandler(console_handler)

    file_handler = RotatingFileHandler("bot.log", maxBytes=10 * 1024 * 1024, backupCount=3, encoding="utf-8")
    file_handler.setFormatter(formatter)
    logger.addHandler(file_handler)


# --- ИНИЦИАЛИЗАЦИЯ БОТА ---
class FixedAiohttpSession(AiohttpSession):
    async def create_session(self) -> aiohttp.ClientSession:
        if self._session is None or self._session.closed:
            if self.proxy:
                connector = ProxyConnector.from_url(self.proxy, family=socket.AF_INET, keepalive_timeout=15)
            else:
                connector = aiohttp.TCPConnector(family=socket.AF_INET, happy_eyeballs_delay=None, keepalive_timeout=15)
            timeout = aiohttp.ClientTimeout(total=None, connect=10, sock_read=30)
            self._session = aiohttp.ClientSession(connector=connector, timeout=timeout, json_serialize=self.json_dumps)
        return self._session


session = FixedAiohttpSession(proxy=config.PROXY_URL) if config.PROXY_URL else FixedAiohttpSession()
bot = Bot(token=config.TELEGRAM_BOT_TOKEN, session=session, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
dp = Dispatcher()


class UserState(StatesGroup):
    waiting_for_birthdate = State()
    waiting_for_tarot_question = State()
    waiting_for_clarification = State()
    waiting_for_partner_birthdate = State()
    waiting_for_admin_search = State()
    waiting_for_broadcast_text = State()
    waiting_for_setting_value = State()
    waiting_for_package_value = State()


class ThrottlingMiddleware(BaseMiddleware):
    """Ограничивает частоту сообщений и нажатий на инлайн-кнопки.

    Кнопки лимитируются отдельно и мягче: за ними нет генерации, но без лимита
    их можно жать очередями, и каждое нажатие плодит новое сообщение бота.
    """

    def __init__(self, limit: float = config.RATE_LIMIT_SECONDS,
                 callback_limit: float = config.CALLBACK_RATE_LIMIT_SECONDS):
        super().__init__()
        self.limit = limit
        self.callback_limit = callback_limit
        self.user_timestamps: dict[int, float] = {}
        self.callback_timestamps: dict[int, float] = {}

    @staticmethod
    def _hit(store: dict[int, float], user_id: int, limit: float) -> bool:
        """True — обращение слишком частое. Иначе засекает время и пропускает."""
        now = time.time()
        if len(store) > 500:
            stale = [uid for uid, t in store.items() if now - t > 60.0]
            for uid in stale:
                del store[uid]

        if now - store.get(user_id, 0.0) < limit:
            return True
        store[user_id] = now
        return False

    async def __call__(self, handler, event, data):
        user = getattr(event, "from_user", None)
        if user is None:
            return await handler(event, data)
        user_id = user.id

        # Безлимит по скорости для администратора
        if user_id == config.ADMIN_ID and config.ADMIN_ID != 0:
            return await handler(event, data)

        if isinstance(event, CallbackQuery):
            if self._hit(self.callback_timestamps, user_id, self.callback_limit):
                # answer() обязателен в любом случае, иначе кнопка «залипает» в спиннере.
                return await event.answer("Не спеши 🕯️")
            return await handler(event, data)

        if not isinstance(event, Message):
            return await handler(event, data)

        text = event.text or ""

        # Пропуск системных кнопок и команд
        if text.startswith("/") or text in config.SYSTEM_BUTTONS:
            return await handler(event, data)

        state: FSMContext = data.get("state")
        if state:
            current_state = await state.get_state()
            if current_state in [UserState.waiting_for_birthdate.state, UserState.waiting_for_partner_birthdate.state]:
                return await handler(event, data)

        if self._hit(self.user_timestamps, user_id, self.limit):
            await event.answer("🕯️ Не спеши, дай мне пару секунд...")
            return

        return await handler(event, data)
