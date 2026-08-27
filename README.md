# Мира — таро-бот в Telegram

Расклады Таро с трактовкой через Gemini, база знаний по книгам (RAG),
внутренняя валюта «свечи», приём платежей и админ-панель внутри бота.

## Что нужно знать перед развёртыванием

**Gemini недоступен с российских IP.** Если сервер в России, переменная
`GEMINI_PROXY_URL` обязательна — без неё не работают ни расклады, ни
эмбеддинги, то есть бот бесполезен целиком. Проверить до запуска:

```bash
python -c "import os,httpx; print(httpx.get('https://generativelanguage.googleapis.com/', proxy=os.environ['GEMINI_PROXY_URL'], timeout=10).status_code)"
```

Ответ `404` или `403` — канал работает (адрес отвечает, просто без ключа).
Таймаут или отказ соединения — прокси не годится.

## Развёртывание

```bash
git clone <репозиторий> && cd ProjectT
cp .env.example .env       # заполнить токен, ключи, ADMIN_ID, прокси
mkdir -p data
docker compose up -d --build
docker compose logs -f
```

Признак успешного старта:

```
🔒 Замок взят
📖 Загружено N фрагментов книг
✨ Мира готова к работе
⏰ [Бэкап] Следующий запуск ... (Europe/Moscow)
```

### Перенос данных со старой машины

База в режиме WAL, поэтому **копировать нужно все три файла**. Если взять один
`mira.db`, приедет состояние на момент последнего чекпойнта — без свежих
раскладов и списаний:

```bash
scp mira.db mira.db-wal mira.db-shm server:/opt/mira/data/
```

Перед переносом стоит вынести базу знаний в отдельный файл — она статична и
занимает 190 МБ из 200:

```bash
python split_knowledge.py          # создать knowledge.db, ничего не удаляя
python split_knowledge.py --drop   # убрать таблицу из mira.db и сжать файл
```

## Эксплуатация

| Что | Как |
|---|---|
| Логи | `docker compose logs -f` или `data/bot.log` |
| Бэкапы | ежедневно в 4:00 в `data/backups`, копия приходит админу в Telegram |
| Ручной бэкап | кнопка «💾 Скачать БД» в `/admin` |
| Обновление | `git pull && docker compose up -d --build` |
| Остановка | `docker compose stop` — бот закроет базу и сбросит WAL |

Бэкап содержит профили, расклады, журнал обращений, **балансы и платежи с
`charge_id`** — без последних возврат звёзд после потери базы невозможен.
База знаний в бэкап не входит: она восстанавливается из CSV.

## Разработка

```bash
python -m venv .venv && .venv/bin/pip install -r requirements.txt
python tests/test_smoke.py          # без обращений к Gemini
python tests/test_smoke.py --deep   # плюс живая проверка фильтра (расходует квоту)
python main.py
```

Второй экземпляр не запустится: мешает файл-замок `LOCK_FILE`. Это защита от
двух поллингов на одном токене — они дают дубли ответов и двойные списания.

## Структура

```
main.py            запуск, поллинг, остановка
bot_core.py        Bot, Dispatcher, логирование, троттлинг
handlers/          menu, spreads, payments, admin, fallback
database.py        SQLite: пользователи, расклады, баланс, журнал
ai_service.py      Gemini: каскад моделей, ключи, фильтр запросов
tasks.py           бэкап и напоминания по расписанию
split_knowledge.py вынос базы знаний в отдельный файл
ingest_books.py    книги (epub/fb2/docx/txt) → knowledge.db
import_chunks.py   CSV → knowledge.db
```

Скриптам подготовки данных нужны дополнительные зависимости:
`pip install -r requirements-ingest.txt`.
