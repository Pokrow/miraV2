import os
import csv
import json
import asyncio
import aiosqlite
from google import genai

import config

MODEL_EMBED = config.MODEL_EMBED
# Фрагменты книг живут в отдельной базе, а не в mira.db с пользователями.
DB_NAME = config.KNOWLEDGE_DB
CSV_FILENAME = os.getenv("CSV_FILENAME", "knowledge_chunks.csv")

ai_client = genai.Client(api_key=config.GEMINI_API_KEYS[0])


async def create_and_fill_knowledge_base():
    if not os.path.exists(CSV_FILENAME):
        print(f"❌ Файл {CSV_FILENAME} не найден в текущей папке!")
        return

    print(f"📂 Открываем {CSV_FILENAME} и подготавливаем базу данных {DB_NAME}...")

    async with aiosqlite.connect(DB_NAME) as db:
        # Создаём таблицу для фрагментов книг
        await db.execute("""
            CREATE TABLE IF NOT EXISTS knowledge_chunks (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                source_title TEXT,
                content TEXT,
                embedding TEXT
            );
        """)
        await db.commit()

        # Читаем CSV-файл
        with open(CSV_FILENAME, mode="r", encoding="utf-8-sig") as f:
            reader = csv.DictReader(f)
            rows = list(reader)

        total = len(rows)
        print(f"📖 Найдено фрагментов для добавления: {total}")

        for idx, row in enumerate(rows, start=1):
            source_title = row.get("source_title") or row.get("title") or "Книга Таро"
            content = row.get("content") or row.get("text") or ""

            if not content.strip():
                continue

            # Проверяем, есть ли уже готовый вектор в CSV, или генерируем через Gemini
            if "embedding" in row and row["embedding"]:
                embedding_json = row["embedding"]
            else:
                try:
                    emb_res = await ai_client.aio.models.embed_content(
                        model=MODEL_EMBED, contents=content
                    )
                    embedding_vector = emb_res.embeddings[0].values
                    embedding_json = json.dumps(embedding_vector)
                except Exception as e:
                    print(f"⚠️ Ошибка создания эмбеддинга для строки {idx}: {e}")
                    continue

            await db.execute(
                "INSERT INTO knowledge_chunks (source_title, content, embedding) VALUES (?, ?, ?)",
                (source_title, content, embedding_json)
            )

            if idx % 10 == 0 or idx == total:
                await db.commit()
                print(f"✅ Обработано {idx}/{total} записей...")

        await db.commit()
        print(f"✨ База знаний успешно сохранена в {DB_NAME}!")


if __name__ == "__main__":
    asyncio.run(create_and_fill_knowledge_base())