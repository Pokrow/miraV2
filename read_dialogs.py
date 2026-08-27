"""Скрипт для просмотра сообщений и раскладов пользователей из базы mira.db."""

import asyncio
import aiosqlite

import config

DB_NAME = config.DB_NAME


async def view_user_activity():
    """Считывает и выводит в консоль все вопросы и диалоги пользователей."""
    try:
        async with aiosqlite.connect(DB_NAME) as db:
            print("=" * 60)
            print("📜 ИСТОРИЯ РАСКЛАДОВ И ВОПРОСОВ (spread_history)")
            print("=" * 60)

            # 1. Получаем все сохраненные расклады
            async with db.execute(
                """
                SELECT id, user_id, spread_type, question, cards_text, created_at 
                FROM spread_history 
                ORDER BY id DESC
                """
            ) as cursor:
                spreads = await cursor.fetchall()

                if not spreads:
                    print("Раскладов пока нет.")
                else:
                    for spread in spreads:
                        s_id, user_id, s_type, question, cards, dt = spread
                        print(f"\n🔹 Расклад #{s_id} | Пользователь ID: {user_id} | Время: {dt}")
                        print(f"   Тип: {s_type}")
                        print(f"   ❓ Вопрос человека: {question}")
                        print(f"   🃏 Выпали карты:\n{cards}")
                        print("-" * 50)

            print("\n" + "=" * 60)
            print("💬 ПОСЛЕДНИЕ ОБРАЩЕНИЯ (request_log)")
            print("=" * 60)

            # 2. Последние 20 обращений: сюда попадают и отклонённые вопросы,
            # и свободный текст мимо сценария — раньше это читалось из
            # user_dialogs, куда никто никогда не писал.
            async with db.execute(
                """
                SELECT user_id, kind, status, question, created_at
                FROM request_log
                ORDER BY id DESC
                LIMIT 20
                """
            ) as cursor:
                dialogs = await cursor.fetchall()

                if not dialogs:
                    print("Обращений пока нет.")
                else:
                    for user_id, kind, status, content, dt in reversed(dialogs):
                        print(f"[{dt}] User {user_id} ({kind} · {status}):\n{content}\n")

    except FileNotFoundError:
        print(f"❌ Файл базы данных {DB_NAME} не найден. Сначала запусти бота, чтобы создать базу.")
    except Exception as e:
        print(f"⚠️ Ошибка при чтении базы данных: {e}")


if __name__ == "__main__":
    asyncio.run(view_user_activity())