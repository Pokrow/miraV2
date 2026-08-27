"""Выносит таблицу knowledge_chunks из основной базы в отдельный knowledge.db.

Зачем: фрагменты книг занимают ~190 МБ из 200 МБ файла mira.db, но статичны и
восстановимы из CSV. Пока они лежат вместе с пользователями, каждый перенос
базы, копия и разбор аварии тащат за собой лишние двести мегабайт.

Запуск в два шага — копирование не трогает исходную базу:

    python split_knowledge.py           # создать knowledge.db и проверить
    python split_knowledge.py --drop    # удалить таблицу из mira.db и сжать файл

Второй шаг необратим, поэтому выполняется только явным флагом и только после
того, как knowledge.db собран и совпал по количеству строк.
"""

import os
import sqlite3
import sys

import config

BATCH = 200  # строки по ~37 КБ: сотнями, чтобы не держать 190 МБ в памяти


def human(nbytes: int) -> str:
    return f"{nbytes / 1024 / 1024:.1f} МБ"


def copy_chunks() -> int:
    if not os.path.exists(config.DB_NAME):
        raise SystemExit(f"Основная база {config.DB_NAME} не найдена.")

    src = sqlite3.connect(f"file:{config.DB_NAME}?mode=ro", uri=True)
    try:
        try:
            total = src.execute("SELECT COUNT(*) FROM knowledge_chunks").fetchone()[0]
        except sqlite3.OperationalError:
            raise SystemExit("В основной базе нет таблицы knowledge_chunks — выносить нечего.")

        if not total:
            raise SystemExit("Таблица knowledge_chunks пуста — выносить нечего.")

        if os.path.exists(config.KNOWLEDGE_DB):
            dst_check = sqlite3.connect(config.KNOWLEDGE_DB)
            try:
                existing = dst_check.execute("SELECT COUNT(*) FROM knowledge_chunks").fetchone()[0]
            except sqlite3.OperationalError:
                existing = 0
            finally:
                dst_check.close()
            if existing:
                print(f"⚠️ {config.KNOWLEDGE_DB} уже содержит {existing} фрагментов — пересобираю с нуля.")
            os.remove(config.KNOWLEDGE_DB)

        print(f"📖 Переношу {total} фрагментов в {config.KNOWLEDGE_DB}...")
        dst = sqlite3.connect(config.KNOWLEDGE_DB)
        try:
            dst.execute("""
                CREATE TABLE knowledge_chunks (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    source_title TEXT,
                    content TEXT,
                    embedding TEXT
                );
            """)
            cursor = src.execute("SELECT id, source_title, content, embedding FROM knowledge_chunks ORDER BY id")
            moved = 0
            while True:
                rows = cursor.fetchmany(BATCH)
                if not rows:
                    break
                dst.executemany(
                    "INSERT INTO knowledge_chunks (id, source_title, content, embedding) VALUES (?, ?, ?, ?)",
                    rows,
                )
                moved += len(rows)
                print(f"   ...{moved}/{total}", end="\r", flush=True)
            dst.commit()

            copied = dst.execute("SELECT COUNT(*) FROM knowledge_chunks").fetchone()[0]
        finally:
            dst.close()
    finally:
        src.close()

    if copied != total:
        raise SystemExit(f"\n❌ Перенесено {copied} из {total} — таблицу из основной базы НЕ трогаю.")

    print(f"\n✅ {config.KNOWLEDGE_DB} готов: {copied} фрагментов, {human(os.path.getsize(config.KNOWLEDGE_DB))}")
    return copied


def drop_from_main(expected: int) -> None:
    """Удаляет таблицу из основной базы. Только после успешной сверки."""
    check = sqlite3.connect(f"file:{config.KNOWLEDGE_DB}?mode=ro", uri=True)
    try:
        copied = check.execute("SELECT COUNT(*) FROM knowledge_chunks").fetchone()[0]
    finally:
        check.close()
    if copied != expected or not copied:
        raise SystemExit("❌ Сверка не сошлась — основную базу не трогаю.")

    before = os.path.getsize(config.DB_NAME)
    conn = sqlite3.connect(config.DB_NAME)
    try:
        conn.execute("DROP TABLE IF EXISTS knowledge_chunks")
        conn.commit()
        # Без VACUUM файл останется прежнего размера: SQLite освобождает
        # страницы внутри файла, но не отдаёт их файловой системе.
        print("🗜 Сжимаю основную базу (VACUUM), это займёт минуту...")
        conn.execute("VACUUM")
        conn.commit()
    finally:
        conn.close()

    after = os.path.getsize(config.DB_NAME)
    print(f"✅ {config.DB_NAME}: {human(before)} → {human(after)}")


if __name__ == "__main__":
    count = copy_chunks()
    if "--drop" in sys.argv:
        drop_from_main(count)
    else:
        print(
            "\nБот уже будет читать базу знаний из нового файла.\n"
            "Убедись, что расклады работают, и удали таблицу из основной базы:\n"
            "    python split_knowledge.py --drop"
        )
