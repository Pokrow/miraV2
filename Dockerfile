# Используем официальный легкий образ Python
FROM python:3.11-slim

# Логи должны появляться в `docker logs` сразу, а не после наполнения буфера.
ENV PYTHONUNBUFFERED=1

# Устанавливаем рабочую директорию внутри контейнера
WORKDIR /app

# Копируем файл с зависимостями и устанавливаем их
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# В образ идут только модули, нужные боту в рантайме, и картинки карт.
# Скрипты подготовки данных (ingest_books, import_chunks, split_knowledge)
# запускаются отдельно и в контейнере не нужны. База, .env и CSV отсечены
# через .dockerignore: данные внутри образа означали бы потерю пользователей
# и балансов при каждом обновлении.
COPY main.py bot_core.py utils.py alerts.py tasks.py config.py database.py keyboards.py ai_service.py ./
COPY handlers ./handlers
COPY tarot_images ./tarot_images

# Базы и бэкапы живут на томе и переживают пересборку образа.
ENV DB_NAME=/app/data/mira.db
ENV KNOWLEDGE_DB=/app/data/knowledge.db
ENV BACKUP_DIR=/app/data/backups
VOLUME ["/app/data"]

# Секреты передаются при запуске: docker run --env-file .env ...
CMD ["python", "main.py"]
