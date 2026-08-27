# Версия зафиксирована: на 3.13 переезжать стоит осознанно, вместе с
# пересборкой и прогоном тестов, а не потому что тег обновился.
FROM python:3.11-slim

# Логи должны появляться в `docker logs` сразу, а не после наполнения буфера.
ENV PYTHONUNBUFFERED=1

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# В образ идут только модули, нужные боту в рантайме, и картинки карт.
# Скрипты подготовки данных (ingest_books, import_chunks, split_knowledge)
# запускаются отдельно и в контейнере не нужны. База, .env и CSV отсечены
# через .dockerignore: данные внутри образа означали бы потерю пользователей
# и балансов при каждом обновлении.
COPY main.py bot_core.py utils.py alerts.py tasks.py single_instance.py \
     config.py database.py keyboards.py ai_service.py ./
COPY handlers ./handlers
COPY tarot_images ./tarot_images

# Базы, бэкапы, логи и файл-замок живут на томе и переживают пересборку образа.
ENV DB_NAME=/app/data/mira.db
ENV KNOWLEDGE_DB=/app/data/knowledge.db
ENV BACKUP_DIR=/app/data/backups
ENV LOG_FILE=/app/data/bot.log
ENV LOCK_FILE=/app/data/mira.lock
VOLUME ["/app/data"]

# Бот не делает ничего, что требует root. Каталог данных заранее отдаётся
# этому пользователю, иначе первая же запись в том упрётся в права.
RUN useradd --create-home --uid 1000 mira \
    && mkdir -p /app/data \
    && chown -R mira:mira /app
USER mira

# Живость проверяем по свежести лога: поллинг пишет в него постоянно, а
# «процесс жив, но висит» — самый частый вид смерти телеграм-бота.
HEALTHCHECK --interval=5m --timeout=10s --start-period=2m --retries=3 \
    CMD python -c "import os,time,sys; f=os.environ['LOG_FILE']; \
sys.exit(0 if os.path.exists(f) and time.time()-os.path.getmtime(f) < 1800 else 1)"

# Секреты передаются при запуске: docker compose --env-file .env
CMD ["python", "main.py"]
