import os
import re
import json
import time
import asyncio
import xml.etree.ElementTree as ET
from pathlib import Path
from bs4 import BeautifulSoup
import ebooklib
from ebooklib import epub
import docx
import httpx  # Добавили импорт httpx для перехвата сетевых сбоев
import aiosqlite
from google import genai

import config

# Ключи читаются из .env. Раньше они лежали здесь открытым текстом — то есть
# попадали в любой репозиторий, архив и образ, куда попадал этот файл.
GEMINI_API_KEYS = config.GEMINI_API_KEYS

# Фрагменты складываются в локальный knowledge.db — тот же файл, из которого
# бот читает базу знаний. Supabase из проекта убран: всё хранится локально.
KNOWLEDGE_DB = config.KNOWLEDGE_DB
BOOKS_DIR = os.getenv("BOOKS_DIR", "./books")

current_key_index = 0
ai_client = genai.Client(api_key=GEMINI_API_KEYS[current_key_index])


# --- 1. ПАРСЕРЫ ФОРМАТОВ ---

def parse_txt(file_path: Path) -> str:
    with open(file_path, "r", encoding="utf-8", errors="ignore") as f:
        return f.read()


def parse_docx(file_path: Path) -> str:
    try:
        doc = docx.Document(file_path)
        return "\n".join([p.text for p in doc.paragraphs if p.text.strip()])
    except Exception as e:
        print(f"   ⚠️ Ошибка чтения .docx: {e}")
        return ""


def parse_fb2(file_path: Path) -> str:
    try:
        tree = ET.parse(file_path)
        root = tree.getroot()
        for elem in root.iter():
            if '}' in elem.tag:
                elem.tag = elem.tag.split('}', 1)[1]

        text_parts = []
        for body in root.findall('body'):
            for p in body.iter('p'):
                if p.text:
                    text_parts.append(p.text)
        return "\n".join(text_parts)
    except Exception as e:
        print(f"   ⚠️ Ошибка чтения .fb2: {e}")
        return ""


def parse_epub(file_path: Path) -> str:
    try:
        book = epub.read_epub(file_path)
        text_parts = []
        for item in book.get_items_of_type(ebooklib.ITEM_DOCUMENT):
            soup = BeautifulSoup(item.get_content(), 'html.parser')
            text_parts.append(soup.get_text())
        return "\n".join(text_parts)
    except Exception as e:
        print(f"   ⚠️ Ошибка чтения .epub: {e}")
        return ""


def extract_text(file_path: Path) -> str:
    ext = file_path.suffix.lower()
    if ext == ".txt":
        return parse_txt(file_path)
    elif ext == ".docx":
        return parse_docx(file_path)
    elif ext == ".fb2":
        return parse_fb2(file_path)
    elif ext == ".epub":
        return parse_epub(file_path)
    else:
        return ""


# --- 2. НАРЕЗКА (CHUNKING) ---

def clean_text(text: str) -> str:
    text = re.sub(r'\s+', ' ', text)
    return text.strip()


def chunk_text(text: str, chunk_size: int = 800, overlap: int = 150) -> list[str]:
    text = clean_text(text)
    if not text:
        return []

    chunks = []
    start = 0
    text_len = len(text)

    while start < text_len:
        end = start + chunk_size
        chunk = text[start:end]
        chunks.append(chunk)
        start += (chunk_size - overlap)

    return chunks


# --- 3. ВЕКТОРИЗАЦИЯ С ЗАЩИТОЙ ОТ СЕТЕВЫХ СБОЕВ И РОТАЦИЕЙ КЛЮЧЕЙ ---

def get_gemini_embeddings_batch(batch_chunks: list[str]) -> list[list[float]]:
    """Пакетная генерация векторов с авто-повтором при сетевых разрывах (SSL/VPN) и смене ключей."""
    global current_key_index, ai_client

    while True:
        try:
            response = ai_client.models.embed_content(
                model=config.MODEL_EMBED,
                contents=batch_chunks
            )
            return [e.values for e in response.embeddings]
        except Exception as e:
            err_msg = str(e)

            # 1. Ловим обрывы VPN / SSL / проблемы сети
            if isinstance(e, httpx.HTTPError) or "SSL" in err_msg or "EOF" in err_msg or "ConnectError" in err_msg:
                print(" 🌐 Сетевой сбой (SSL/VPN). Пробуем снова через 5 секунд...")
                time.sleep(5)
                continue

            # 2. Ловим лимиты (429) и проблемы доступа (403)
            if "429" in err_msg or "RESOURCE_EXHAUSTED" in err_msg or "403" in err_msg or "PERMISSION_DENIED" in err_msg:
                if current_key_index + 1 < len(GEMINI_API_KEYS):
                    current_key_index += 1
                    next_key = GEMINI_API_KEYS[current_key_index]
                    print(
                        f"\n🔄 Лимит ключа №{current_key_index} исчерпан. Переключаемся на ключ №{current_key_index + 1}...")

                    ai_client = genai.Client(api_key=next_key)
                    time.sleep(1.0)
                else:
                    print(" ⏳ Все добавленные API-ключи исчерпали лимиты! Пауза 60 секунд...")
                    time.sleep(60)
            else:
                print(f" ⚠️ Непредвиденная ошибка: {e}. Повтор через 5 секунд...")
                time.sleep(5)


# --- 4. ОСНОВНОЙ ЦИКЛ ЗАГРУЗКИ ---

async def process_and_save_books():
    print(f"🔌 Открываем {KNOWLEDGE_DB} (Активен API-ключ №{current_key_index + 1} из {len(GEMINI_API_KEYS)})...")
    conn = await aiosqlite.connect(KNOWLEDGE_DB)

    # Схема ровно та, которую читает бот: вектор хранится строкой JSON.
    await conn.execute("""
        CREATE TABLE IF NOT EXISTS knowledge_chunks (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            source_title TEXT,
            content TEXT NOT NULL,
            embedding TEXT
        );
    """)
    await conn.commit()

    books_folder = Path(BOOKS_DIR)

    if not books_folder.exists():
        print(f"❌ Ошибка: Папка '{BOOKS_DIR}' не найдена!")
        await conn.close()
        return

    for file_path in books_folder.iterdir():
        if file_path.is_file():
            print(f"\n📖 Обрабатываем книгу: {file_path.name}...")
            raw_text = extract_text(file_path)

            if not raw_text:
                print("   ⚠️ Пустой или нечитаемый файл. Пропускаем.")
                continue

            chunks = chunk_text(raw_text, chunk_size=800, overlap=150)

            async with conn.execute(
                "SELECT COUNT(*) FROM knowledge_chunks WHERE source_title = ?",
                (file_path.name,)
            ) as cursor:
                saved_count = (await cursor.fetchone())[0]

            if saved_count >= len(chunks):
                print(f"   ⏩ Книга уже полностью загружена ({saved_count}/{len(chunks)} чанков). Пропускаем!")
                continue
            elif saved_count > 0:
                print(f"   🔄 Возобновляем загрузку с чанка №{saved_count} (всего {len(chunks)})...")
                chunks_to_process = chunks[saved_count:]
            else:
                print(f"   ✂️ Нарезано на {len(chunks)} фрагментов. Начинаем оцифровку...")
                chunks_to_process = chunks

            batch_size = 10
            for i in range(0, len(chunks_to_process), batch_size):
                batch_chunks = chunks_to_process[i:i + batch_size]

                embeddings = get_gemini_embeddings_batch(batch_chunks)

                await conn.executemany(
                    """
                    INSERT INTO knowledge_chunks (source_title, content, embedding)
                    VALUES (?, ?, ?)
                    """,
                    [(file_path.name, chunk, json.dumps(list(emb)))
                     for chunk, emb in zip(batch_chunks, embeddings)],
                )
                # Коммит после каждой пачки: обрыв на середине книги не должен
                # терять уже оцифрованное — на нём же строится докачка.
                await conn.commit()

                current_total = saved_count + i + len(batch_chunks)
                print(f"   Загружено {min(current_total, len(chunks))}/{len(chunks)}...")

                await asyncio.sleep(4.5)

    await conn.commit()
    await conn.close()
    print(f"\n✨ Хроники Акаши запечатаны! Все книги загружены в {KNOWLEDGE_DB}.")
    print("Перезапусти бота, чтобы он подхватил новые фрагменты в память.")


if __name__ == "__main__":
    asyncio.run(process_and_save_books())