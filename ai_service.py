"""Сервисы искусственного интеллекта и медиа-обработки.

Включает управление API-ключами Gemini, каскад моделей, обработку изображений,
лунный календарь и фильтрацию сообщений.
"""

import asyncio
import html
import logging
import os
import random
import re
import time
from datetime import date
from io import BytesIO

from aiogram import Bot
from aiogram.types import BufferedInputFile, FSInputFile
from google import genai
from google.genai import errors
from PIL import Image

import config

logger = logging.getLogger("mira_bot")

# Множество ID пользователей, чьи запросы сейчас находятся в обработке
processing_users: set[int] = set()


class GeminiKeyManager:
    """Менеджер пула API-ключей Gemini с авто-ротацией при исчерпании лимитов."""

    def __init__(self, keys: list[str]):
        self.keys = keys
        self.current_idx = 0

    def get_client(self) -> genai.Client:
        return genai.Client(api_key=self.keys[self.current_idx])

    def get_client_at(self, idx: int) -> genai.Client:
        """Клиент для конкретного индекса ключа, независимо от общего current_idx."""
        return genai.Client(api_key=self.keys[idx % len(self.keys)])

    def rotate_key(self) -> str:
        old_idx = self.current_idx
        self.current_idx = (self.current_idx + 1) % len(self.keys)
        logger.warning(
            f"🔄 [KeyManager] Переключение с Ключа #{old_idx + 1} на Ключ #{self.current_idx + 1}."
        )
        return self.keys[self.current_idx]


key_manager = GeminiKeyManager(config.GEMINI_API_KEYS)

# Общий на весь бот ограничитель одновременных обращений к Gemini.
# Без него десять клиентов сразу дают десять параллельных запросов — и 429
# прилетает по всем ключам одновременно, вместо того чтобы просто подождать
# очереди. Создаётся вне цикла событий: с Python 3.10 семафор к нему не привязан.
_gemini_gate = asyncio.Semaphore(config.GEMINI_MAX_CONCURRENCY)


async def keep_typing(chat_id: int, bot_instance: Bot):
    """Имитирует статус 'печатает...' каждые 4 секунды, пока генерируется ответ."""
    while True:
        try:
            await bot_instance.send_chat_action(chat_id=chat_id, action="typing")
            await asyncio.sleep(4)
        except asyncio.CancelledError:
            break
        except Exception:
            break


async def call_gemini_with_retry(full_prompt: str) -> str:
    """Выполняет запрос к Gemini, перебирая модели каскада и ключи при ошибках квот.

    Индекс ключа для перебора считается локально (start_idx + offset), чтобы
    ротация ключа другим параллельным запросом не срывала гарантию перебора
    всех ключей в рамках этого конкретного вызова.
    """
    async with _gemini_gate:
        return await _call_gemini_unguarded(full_prompt)


async def _call_gemini_unguarded(full_prompt: str) -> str:
    for model_name in config.MODELS_CASCADE:
        start_idx = key_manager.current_idx
        for offset in range(len(key_manager.keys)):
            idx = (start_idx + offset) % len(key_manager.keys)
            client = key_manager.get_client_at(idx)
            try:
                logger.info(f"🤖 [Gemini] Запрос к {model_name} (Ключ #{idx + 1})...")
                start_t = time.time()
                response = await client.aio.models.generate_content(
                    model=model_name, contents=full_prompt
                )
                elapsed = time.time() - start_t
                logger.info(f"✨ [Gemini] Ответ получен за {elapsed:.2f} сек.")
                return response.text.strip()
            except errors.ServerError as se:
                # 5xx (503 UNAVAILABLE / 500) — перегружена сама модель на стороне Google.
                # Это состояние общее для всех ключей и проектов, перебор ключей бесполезен:
                # сразу уходим на следующую модель каскада.
                logger.warning(
                    f"⚠️ Модель {model_name} перегружена ({getattr(se, 'code', '5xx')}). "
                    f"Переход к следующей модели каскада..."
                )
                break
            except errors.ClientError as ce:
                code = getattr(ce, "code", None)
                if code == 429 or "RESOURCE_EXHAUSTED" in str(ce):
                    logger.warning(
                        f"⚠️ Лимит на ключе #{idx + 1} для {model_name}. Пробуем следующий ключ..."
                    )
                    key_manager.rotate_key()
                    await asyncio.sleep(1)
                elif code == 404 or "NOT_FOUND" in str(ce):
                    logger.warning(
                        f"⚠️ Модель {model_name} недоступна (404). Переход к следующей модели каскада..."
                    )
                    break
                else:
                    logger.error(f"❌ Ошибка вызова Gemini ({model_name}): {ce}")
                    await asyncio.sleep(1)
            except Exception as e:
                logger.warning(f"⚠️ Ошибка сети Gemini ({model_name}): {e}")
                await asyncio.sleep(1)

    raise RuntimeError("Все доступные ключи и модели каскада исчерпали свои лимиты на сегодня.")


async def get_embedding_safe(text: str) -> list[float] | None:
    """Безопасно генерирует вектор текста. Возвращает None, если все ключи недоступны."""
    async with _gemini_gate:
        return await _embed_unguarded(text)


async def _embed_unguarded(text: str) -> list[float] | None:
    start_idx = key_manager.current_idx
    for offset in range(len(key_manager.keys)):
        idx = (start_idx + offset) % len(key_manager.keys)
        client = key_manager.get_client_at(idx)
        try:
            emb_res = await client.aio.models.embed_content(
                model=config.MODEL_EMBED, contents=text
            )
            return emb_res.embeddings[0].values
        except errors.ServerError as se:
            # Модель эмбеддингов перегружена — состояние общее для всех ключей,
            # перебирать их смысла нет.
            logger.warning(
                f"⚠️ [Embedding] Сервис перегружен ({getattr(se, 'code', '5xx')}). RAG пропущен."
            )
            return None
        except errors.ClientError as ce:
            if getattr(ce, "code", None) == 429 or "RESOURCE_EXHAUSTED" in str(ce):
                logger.warning(f"⚠️ [Embedding] Лимит на ключе #{idx + 1}.")
                key_manager.rotate_key()
            else:
                logger.warning(f"⚠️ [Embedding] Ошибка на ключе #{idx + 1}: {ce}")
        except Exception as e:
            logger.warning(f"⚠️ [Embedding] Сетевая ошибка на ключе #{idx + 1}: {e}")
    logger.warning("⚠️ Не удалось получить эмбеддинг. RAG временно пропущен.")
    return None


def format_ai_response(text: str) -> str:
    """Экранирует HTML-символы и сохраняет теги форматирования <b> и <i>."""
    if not text:
        return ""
    formatted = re.sub(r"\*\*(.+?)\*\*", r"<b>\1</b>", text, flags=re.DOTALL)
    formatted = re.sub(r"(?<!\*)\*([^*]+?)\*(?!\*)", r"<i>\1</i>", formatted, flags=re.DOTALL)
    formatted = re.sub(r"(?<!_)_([^_]+?)_(?!_)", r"<i>\1</i>", formatted, flags=re.DOTALL)
    formatted = re.sub(r"<b\s*>", "<b>", formatted, flags=re.IGNORECASE)
    formatted = re.sub(r"</b\s*>", "</b>", formatted, flags=re.IGNORECASE)
    formatted = re.sub(r"<i\s*>", "<i>", formatted, flags=re.IGNORECASE)
    formatted = re.sub(r"</i\s*>", "</i>", formatted, flags=re.IGNORECASE)

    placeholders = {
        "<b>": "___HTML_TAG_B_OPEN___",
        "</b>": "___HTML_TAG_B_CLOSE___",
        "<i>": "___HTML_TAG_I_OPEN___",
        "</i>": "___HTML_TAG_I_CLOSE___",
    }
    for tag, ph in placeholders.items():
        formatted = formatted.replace(tag, ph)
    formatted = html.escape(formatted)
    for tag, ph in placeholders.items():
        formatted = formatted.replace(ph, tag)
    return formatted


def process_card_image_sync(fpath: str, is_rev: bool) -> BufferedInputFile | FSInputFile | None:
    """Синхронно открывает и переворачивает изображение карты при необходимости."""
    if not os.path.exists(fpath):
        return None
    if is_rev:
        try:
            with Image.open(fpath) as img:
                img_rgb = img.convert("RGB")
                pil_img = img_rgb.rotate(180)
                img_stream = BytesIO()
                pil_img.save(img_stream, format="JPEG", quality=85)
                img_stream.seek(0)
                fname = os.path.basename(fpath)
                return BufferedInputFile(img_stream.read(), filename=f"rev_{fname}")
        except Exception as img_err:
            logger.error(f"Ошибка поворота {fpath}: {img_err}", exc_info=True)
            return FSInputFile(fpath)
    return FSInputFile(fpath)


def get_moon_phase(dt: date = None) -> str:
    """Вычисляет текущую фазу луны по синодическому циклу."""
    if dt is None:
        dt = date.today()
    new_moon = date(2000, 1, 6)
    days = (dt - new_moon).days
    lunations = days / 29.530588
    phase = lunations % 1

    if phase < 0.03 or phase > 0.97:
        return "🌑 Новолуние (время тишины и закладки намерений)"
    elif phase < 0.25:
        return "🌒 Растущая Луна (время роста, действий и развития)"
    elif phase < 0.47:
        return "🌓 Первая четверть (время преодоления сомнений)"
    elif phase < 0.53:
        return "🌕 Полнолуние (время ясности, интуиции и высвобождения чувств)"
    elif phase < 0.75:
        return "🌖 Убывающая Луна (время отпускания и переосмысления)"
    else:
        return "🌘 Последняя четверть (время очищения и внутренней тишины)"


# Короткие служебные слова, которые человек пишет вместо вопроса. Через оба
# фильтра их пропускаем без разбора: смысла в них нет по определению.
TRIGGER_WORDS = {"тяни", "общее", "расклад", "карта", "совет", "да", "нет"}


def validate_question_fast(user_query: str) -> tuple[bool, str]:
    """Проверяет запрос на длину, абракадабру и бытовые оффтоп-темы."""
    query_clean = user_query.strip()
    query_lower = query_clean.lower()

    if len(query_clean) > config.MAX_QUESTION_LENGTH:
        return (
            False,
            f"🕯️ Твой вопрос слишком объёмный. Сформулируй его чуть короче (до {config.MAX_QUESTION_LENGTH} символов).",
        )

    if query_lower in TRIGGER_WORDS:
        return True, ""

    # Сообщения без слов («?», «...», «а», одни эмодзи) вопросом не считаются.
    if sum(1 for ch in query_clean if ch.isalpha()) < 3:
        return False, config.REFUSAL_BY_REASON["NONSENSE"]

    words = query_lower.split()
    cyrillic_vowels = set("аеёиоуыэюя")
    latin_vowels = set("aeiouy")

    for w in words:
        has_cyr = bool(re.search(r"[а-яё]", w))
        has_lat = bool(re.search(r"[a-z]", w))
        if has_cyr and has_lat:
            return (
                False,
                "Не могу уловить суть твоего вопроса 🕯️ Напиши свой жизненный запрос словами или просто скажи <b>«тяни»</b>.",
            )
        if re.search(r"(.)\1{2,}", w):
            return False, "Сформулируй свой вопрос яснее 🕯️ Или просто напиши <b>«тяни»</b>."

        letters = [ch for ch in w if ch.isalpha()]
        if len(letters) >= 4:
            v_count = sum(1 for ch in letters if (ch in cyrillic_vowels or ch in latin_vowels))
            if v_count == 0:
                return (
                    False,
                    "Не могу уловить суть твоего вопроса 🕯️ Напиши свой жизненный запрос словами или просто скажи <b>«тяни»</b>.",
                )
            ratio = v_count / len(letters)
            if (ratio < 0.18 or ratio > 0.85) and len(letters) >= 6:
                return False, "Не могу разобрать слова запроса 🕯️ Сформулируй свой вопрос или напиши <b>«тяни»</b>."

    # Категории мусора, которые ловятся по словам — без обращения к модели.
    # Порядок важен: первая сработавшая категория определяет ответ.
    pattern_groups: list[tuple[str, list[str]]] = [
        ("TEST", [
            r"\bты (бот|робот|нейросет|ии|программ|живая|настоящ|человек)",
            r"\b(нейросет|чат ?гпт|chatgpt|gpt|gemini|джемини)\b",
            r"\bкто (тебя|тя) (создал|написал|сделал)",
            r"\bсистемн\w* (промпт|инструкц)",
            r"\bигнорируй\b.*\bинструкц",
            r"\bповтори за мной\b",
            r"\bкакая у тебя модель\b",
            r"^кто ты",
        ]),
        ("JOKE", [
            r"\bанекдот",
            r"\bпошути",
            r"\bприкол\w*\b",
            r"\bржак",
            r"\bхаха+\b",
            r"\bлол\b",
            r"\bмем\b",
            r"\bтроллю\b",
        ]),
        ("FACT", [
            r"\bлотере",
            r"\bвыигрышн\w* (номер|числ)",
            r"\bкурс (доллар|евро|валют|биткоин)",
            r"\bсколько будет\b",
            r"\bсчёт матча\b|\bсчет матча\b",
            r"\bкакие числа\b",
            r"\bномера\b.*\bбилет",
            r"\bставк\w* на (спорт|матч)",
        ]),
        ("SENSITIVE", [
            r"\bкогда я умру\b",
            r"\b(умрёт|умрет|погибнет)\b",
            r"\bдиагноз\b",
            r"\b(рак|онколог|опухол)\w*\b",
            r"\bбеременн\w*\s+(ли|я)\b",
            r"\bкак(ое|ие)? лекарств",
            r"\bсуицид|\bпокончить с собой|\bубить себя",
            r"\bнаркотик",
        ]),
        ("OFFTOPIC", [
            r"\bшашлык",
            r"\bдоставк",
            r"\bпицц",
            r"\bролл",
            r"\bсуши",
            r"\bбургер",
            r"\bед[аыу]\b",
            r"\bпогод",
            r"\bремонт",
            r"\bпрограммир",
            r"\bкод\b",
            r"\bпитон",
            r"\bpython",
            r"\bзадач",
            r"\bкурьер",
            r"\bрецепт",
            r"\bприготов",
            r"\bперевед[иё]\b",
            r"\bнапиши (текст|письмо|сочинение|статью)",
            r"\bсделай уроки\b",
        ]),
        ("GREETING", [
            r"^как дела",
            r"^как ты\b",
            r"^что делаешь",
            r"^привет",
            r"^добрый (день|вечер|утро)",
            r"^доброе утро",
            r"^как жизнь",
            r"^ку\b",
            r"^хай\b",
        ]),
    ]

    for reason, patterns in pattern_groups:
        for pattern in patterns:
            if re.search(pattern, query_lower):
                if reason == "GREETING":
                    return (
                        False,
                        "Для расклада нужен твой жизненный вопрос или ситуация 🕯️ Сформулируй свой запрос или напиши <b>«общее»</b>.",
                    )
                if reason == "OFFTOPIC":
                    return False, random.choice(config.REFUSAL_TEXTS)
                return False, config.REFUSAL_BY_REASON[reason]
    return True, ""


async def _ask_guard_model(prompt: str) -> str | None:
    """Один короткий вызов модели-привратника. None — проверку сделать не удалось.

    Ключи перебираются, но без ротации общего указателя: привратник не должен
    сдвигать очередь ключей для основной генерации. При любой ошибке или
    таймауте возвращается None — фильтр работает «на пропуск», чтобы сбой
    Gemini не превращался в отказ живому клиенту.
    """
    start_idx = key_manager.current_idx
    for offset in range(min(2, len(key_manager.keys))):
        idx = (start_idx + offset) % len(key_manager.keys)
        client = key_manager.get_client_at(idx)
        try:
            # Привратник намеренно идёт мимо общего семафора: это короткий
            # вызов перед списанием свечей, и он не должен стоять в очереди
            # за длинными генерациями.
            response = await asyncio.wait_for(
                client.aio.models.generate_content(
                    model=config.MODEL_GUARD, contents=prompt
                ),
                timeout=config.GUARD_TIMEOUT_SECONDS,
            )
            return (response.text or "").strip()
        except asyncio.TimeoutError:
            logger.warning("⚠️ [Guard] Проверка смысла не уложилась в таймаут — вопрос пропущен.")
            return None
        except errors.ServerError:
            logger.warning("⚠️ [Guard] Модель-привратник перегружена — вопрос пропущен.")
            return None
        except Exception as e:
            logger.warning(f"⚠️ [Guard] Ошибка на ключе #{idx + 1}: {e}")
    return None


async def validate_question_deep(user_query: str) -> tuple[bool, str]:
    """Смысловая проверка запроса моделью — то, что не ловится по словам.

    Вызывается ПОСЛЕ быстрой проверки и ДО списания свечей. Возвращает
    (можно ли делать расклад, текст отказа).
    """
    query_clean = user_query.strip()
    if query_clean.lower() in TRIGGER_WORDS:
        return True, ""

    verdict = await _ask_guard_model(
        config.GUARD_PROMPT.format(query=config.fence_user_text(query_clean))
    )
    if not verdict:
        return True, ""

    # Модель иногда добавляет точку или кавычки — берём первое слово-код.
    token = re.sub(r"[^A-Z_]", " ", verdict.upper()).split()
    reason = token[0] if token else "OK"

    if reason == "OK":
        return True, ""
    if reason in config.REFUSAL_BY_REASON:
        logger.info(f"🚧 [Guard] Запрос отклонён как {reason}: {query_clean[:60]!r}")
        return False, config.REFUSAL_BY_REASON[reason]

    # Неизвестный код — считать это отказом нельзя: ошибётся модель, пострадает клиент.
    logger.warning(f"⚠️ [Guard] Непонятный вердикт {verdict!r} — вопрос пропущен.")
    return True, ""