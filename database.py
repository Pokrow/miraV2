import aiosqlite
import json
import logging
import os
from datetime import date, datetime, timedelta

import numpy as np

import config

logger = logging.getLogger("mira_bot")

db_conn: aiosqlite.Connection | None = None
PHOTO_CACHE: dict[str, str] = {}

# Матрица эмбеддингов (N x dim), заранее нормированная по строкам, и параллельные
# ей заголовки/тексты. Позволяет считать все косинусные близости одним matvec
# вместо поштучного перебора на чистом Python.
#
# Векторы храним ТОЛЬКО как float32-матрицу: те же данные в виде списков Python
# занимают на порядок больше памяти (каждый float — отдельный объект).
KNOWLEDGE_MATRIX: np.ndarray | None = None
KNOWLEDGE_META: list[tuple[str, str]] = []


async def init_db() -> None:
    global db_conn
    db_conn = await aiosqlite.connect(config.DB_NAME)
    await db_conn.execute("PRAGMA journal_mode=WAL;")

    await db_conn.execute(
        "CREATE TABLE IF NOT EXISTS users (user_id INTEGER PRIMARY KEY, birth_date TEXT, birth_arcana TEXT, last_spread TEXT);")
    await db_conn.execute(
        "CREATE TABLE IF NOT EXISTS user_dialogs (id INTEGER PRIMARY KEY AUTOINCREMENT, user_id INTEGER NOT NULL, role TEXT NOT NULL, content TEXT NOT NULL, created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP);")
    await db_conn.execute(
        "CREATE TABLE IF NOT EXISTS user_limits (user_id INTEGER PRIMARY KEY, last_card_date TEXT, last_spread_date TEXT);")
    await db_conn.execute("CREATE TABLE IF NOT EXISTS photo_cache (file_name TEXT PRIMARY KEY, file_id TEXT NOT NULL);")
    await db_conn.execute(
        "CREATE TABLE IF NOT EXISTS knowledge_chunks (id INTEGER PRIMARY KEY AUTOINCREMENT, source_title TEXT, content TEXT, embedding TEXT);")
    await db_conn.execute("""
                          CREATE TABLE IF NOT EXISTS spread_history
                          (
                              id
                              INTEGER
                              PRIMARY
                              KEY
                              AUTOINCREMENT,
                              user_id
                              INTEGER
                              NOT
                              NULL,
                              spread_type
                              TEXT
                              NOT
                              NULL,
                              question
                              TEXT
                              NOT
                              NULL,
                              question_embedding
                              TEXT,
                              cards_text
                              TEXT
                              NOT
                              NULL,
                              answer_text
                              TEXT
                              NOT
                              NULL,
                              created_at
                              TIMESTAMP
                              DEFAULT
                              CURRENT_TIMESTAMP
                          );
                          """)
    # Колонки, добавленные после первого релиза: на живой базе их нет, поэтому
    # каждая доливается отдельно и молча пропускается, если уже есть.
    for migration in (
        "ALTER TABLE spread_history ADD COLUMN question_embedding TEXT;",
        # Счётчик уточнений: одно на расклад, поэтому хранить его надо у расклада.
        "ALTER TABLE spread_history ADD COLUMN clarify_count INTEGER DEFAULT 0;",
        # Счётчик бесплатных перефразировок того же вопроса за сутки.
        "ALTER TABLE spread_history ADD COLUMN repeat_count INTEGER DEFAULT 0;",
        # Пол клиента: без него модель по умолчанию обращается в женском роде.
        "ALTER TABLE users ADD COLUMN gender TEXT;",
        # Заблокировавшие бота: иначе каждая рассылка снова стучится к ним.
        "ALTER TABLE users ADD COLUMN is_blocked INTEGER DEFAULT 0;",
        # Напоминание о бесплатной карте дня — строго по согласию.
        "ALTER TABLE users ADD COLUMN daily_reminder INTEGER DEFAULT 0;",
        "ALTER TABLE users ADD COLUMN last_reminded TEXT;",
    ):
        try:
            await db_conn.execute(migration)
        except aiosqlite.OperationalError:
            pass

    # Журнал ВСЕХ обращений: расклады, уточнения, совместимость и отклонённые
    # вопросы. spread_history хранит только состоявшиеся расклады, поэтому
    # админка по нему видела лишь часть картины.
    await db_conn.execute("""
        CREATE TABLE IF NOT EXISTS request_log (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL,
            kind TEXT NOT NULL,
            question TEXT NOT NULL,
            status TEXT NOT NULL DEFAULT 'ok',
            note TEXT,
            spread_id INTEGER,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        );
    """)
    await db_conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_reqlog_user ON request_log(user_id, id DESC);"
    )
    await _backfill_request_log()

    # --- БАЛАНС, ПЛАТЕЖИ И РЕФЕРАЛЫ ---
    await db_conn.execute("""
        CREATE TABLE IF NOT EXISTS balances (
            user_id INTEGER PRIMARY KEY,
            candles INTEGER NOT NULL DEFAULT 0,
            welcome_given INTEGER DEFAULT 0,
            updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        );
    """)
    # Полный журнал движений баланса: без него спорную ситуацию с пользователем
    # («у меня списалось, а расклада нет») разобрать нечем.
    await db_conn.execute("""
        CREATE TABLE IF NOT EXISTS transactions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL,
            amount INTEGER NOT NULL,
            balance_after INTEGER NOT NULL,
            kind TEXT NOT NULL,
            note TEXT,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        );
    """)
    await db_conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_tx_user ON transactions(user_id, id DESC);"
    )
    # Настройки, редактируемые из админки без перезапуска.
    await db_conn.execute("""
        CREATE TABLE IF NOT EXISTS settings (
            key TEXT PRIMARY KEY,
            value TEXT NOT NULL,
            updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        );
    """)
    await db_conn.execute("""
        CREATE TABLE IF NOT EXISTS topup_packages (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            title TEXT NOT NULL,
            candles INTEGER NOT NULL,
            stars INTEGER NOT NULL,
            rub INTEGER NOT NULL,
            active INTEGER DEFAULT 1,
            sort_order INTEGER DEFAULT 0
        );
    """)
    # charge_id нужен для возврата звёзд через refundStarPayment; UNIQUE защищает
    # от повторного начисления, если Telegram доставит апдейт об оплате дважды.
    await db_conn.execute("""
        CREATE TABLE IF NOT EXISTS payments (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL,
            charge_id TEXT UNIQUE,
            amount INTEGER NOT NULL,
            currency TEXT DEFAULT 'XTR',
            candles INTEGER NOT NULL DEFAULT 0,
            provider TEXT,
            refunded INTEGER DEFAULT 0,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        );
    """)
    # Одна строка на приглашённого: связь фиксируется один раз и не переписывается.
    await db_conn.execute("""
        CREATE TABLE IF NOT EXISTS referrals (
            user_id INTEGER PRIMARY KEY,
            inviter_id INTEGER NOT NULL,
            rewarded INTEGER DEFAULT 0,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        );
    """)
    await db_conn.commit()
    await _seed_defaults()

    async with db_conn.execute("SELECT file_name, file_id FROM photo_cache") as cursor:
        for r in await cursor.fetchall():
            PHOTO_CACHE[r[0]] = r[1]
    logger.info(f"📸 Загружено {len(PHOTO_CACHE)} кэшированных фото карт из БД.")

    await _load_knowledge_cache()


async def _backfill_request_log() -> None:
    """Переносит уже накопленные расклады в журнал обращений — один раз.

    Без этого админка после обновления показала бы пустую ленту: старые запросы
    жили только в spread_history.
    """
    async with db_conn.execute("SELECT COUNT(*) FROM request_log") as cursor:
        if (await cursor.fetchone())[0]:
            return

    await db_conn.execute("""
        INSERT INTO request_log (user_id, kind, question, status, note, spread_id, created_at)
        SELECT user_id, spread_type, question, 'ok', '', id, created_at
        FROM spread_history ORDER BY id
    """)
    await db_conn.commit()

    async with db_conn.execute("SELECT COUNT(*) FROM request_log") as cursor:
        moved = (await cursor.fetchone())[0]
    if moved:
        logger.info(f"🗂 Журнал обращений заполнен из истории раскладов: {moved} записей.")


async def _load_knowledge_cache() -> None:
    """Выбирает источник базы знаний и прогревает кэш RAG.

    Отдельный knowledge.db — основной вариант; чтение из основной базы
    оставлено для случая, когда файл ещё не выделен (split_knowledge.py).
    """
    if os.path.exists(config.KNOWLEDGE_DB):
        # Только чтение: база знаний статична, и портить её боту незачем.
        source = await aiosqlite.connect(f"file:{config.KNOWLEDGE_DB}?mode=ro", uri=True)
        try:
            await _load_knowledge_from(source, config.KNOWLEDGE_DB)
        finally:
            await source.close()
    else:
        await _load_knowledge_from(db_conn, config.DB_NAME)


async def _load_knowledge_from(conn: aiosqlite.Connection, label: str) -> None:
    """Загружает фрагменты книг и строит нормированную матрицу эмбеддингов для RAG.

    Матрица заполняется построчно в заранее выделенный float32-буфер: держать
    5000+ векторов по 3072 значения в виде списков Python стоило бы сотен мегабайт.
    """
    global KNOWLEDGE_MATRIX, KNOWLEDGE_META

    KNOWLEDGE_MATRIX = None
    KNOWLEDGE_META = []

    try:
        async with conn.execute(
            "SELECT COUNT(*) FROM knowledge_chunks WHERE embedding IS NOT NULL AND embedding != ''"
        ) as cursor:
            total = (await cursor.fetchone())[0]
    except aiosqlite.OperationalError:
        logger.info(f"📖 В {label} нет таблицы knowledge_chunks — RAG отключён.")
        return

    if not total:
        logger.info("📖 База знаний пуста — RAG отключён.")
        return

    matrix: np.ndarray | None = None
    meta: list[tuple[str, str]] = []
    row_i = 0

    async with conn.execute("SELECT source_title, content, embedding FROM knowledge_chunks") as cursor:
        async for title, content, emb in cursor:
            if not emb:
                continue
            try:
                vec = np.asarray(json.loads(emb), dtype=np.float32)
            except Exception:
                continue
            if vec.ndim != 1 or vec.size == 0:
                continue

            if matrix is None:
                matrix = np.empty((total, vec.size), dtype=np.float32)
            elif vec.size != matrix.shape[1]:
                # Фрагмент с чужой размерностью (сменилась модель эмбеддингов) — пропускаем.
                continue
            if row_i >= matrix.shape[0]:
                break

            matrix[row_i] = vec
            meta.append((title, content))
            row_i += 1

    if matrix is None or row_i == 0:
        logger.warning("📖 Не удалось загрузить ни одного валидного эмбеддинга — RAG отключён.")
        return

    matrix = matrix[:row_i]
    # Нормируем один раз при старте, чтобы косинус свёлся к скалярному произведению.
    norms = np.linalg.norm(matrix, axis=1, keepdims=True)
    norms[norms == 0] = 1.0
    matrix /= norms

    KNOWLEDGE_MATRIX = matrix
    KNOWLEDGE_META = meta
    logger.info(
        f"📖 Загружено {row_i} фрагментов книг из {os.path.basename(label)} "
        f"({matrix.nbytes / 1024 / 1024:.0f} МБ, dim={matrix.shape[1]})."
    )


async def save_photo_cache(file_name: str, file_id: str) -> None:
    PHOTO_CACHE[file_name] = file_id
    await db_conn.execute(
        "INSERT INTO photo_cache (file_name, file_id) VALUES (?, ?) ON CONFLICT(file_name) DO UPDATE SET file_id = ?",
        (file_name, file_id, file_id))
    await db_conn.commit()


async def save_spread_record(user_id: int, spread_type: str, question: str, q_embedding: str, cards_text: str,
                             answer_text: str) -> int:
    cursor = await db_conn.execute(
        "INSERT INTO spread_history (user_id, spread_type, question, question_embedding, cards_text, answer_text) VALUES (?, ?, ?, ?, ?, ?)",
        (user_id, spread_type, question, q_embedding, cards_text, answer_text),
    )
    await db_conn.commit()
    return cursor.lastrowid


async def get_today_spreads(user_id: int, spread_type: str) -> list[dict]:
    today_str = date.today().isoformat()
    async with db_conn.execute(
            # Порядок важен: повтор всегда привязывается к САМОМУ РАННЕМУ
            # совпавшему раскладу, иначе счётчик повторов обнулялся бы на
            # каждой новой записи и лимит не работал бы вовсе.
            "SELECT id, question, question_embedding, cards_text, answer_text FROM spread_history WHERE user_id = ? AND spread_type = ? AND date(created_at) = ? ORDER BY id",
            (user_id, spread_type, today_str),
    ) as cursor:
        rows = await cursor.fetchall()
        return [{"id": r[0], "question": r[1], "embedding": json.loads(r[2]) if r[2] else [], "cards_text": r[3],
                 "answer_text": r[4]} for r in rows]


async def get_spread_by_id(spread_id: int) -> dict | None:
    async with db_conn.execute(
            "SELECT id, spread_type, question, cards_text, answer_text, created_at, user_id FROM spread_history WHERE id = ?",
            (spread_id,)) as cursor:
        row = await cursor.fetchone()
        return {"id": row[0], "spread_type": row[1], "question": row[2], "cards_text": row[3], "answer_text": row[4],
                "created_at": row[5], "user_id": row[6]} if row else None


async def get_clarify_count(spread_id: int) -> int:
    """Сколько уточнений уже задано по раскладу."""
    async with db_conn.execute(
        "SELECT COALESCE(clarify_count, 0) FROM spread_history WHERE id = ?", (spread_id,)
    ) as cursor:
        row = await cursor.fetchone()
        return row[0] if row else 0


async def clarification_available(spread_id: int) -> bool:
    """Уточнение бесплатное, но одно на расклад — иначе это бесконечный чат с моделью."""
    return await get_clarify_count(spread_id) < config.CLARIFICATIONS_PER_SPREAD


async def mark_clarification_used(spread_id: int) -> None:
    """Отмечается только после доставленного ответа: за сбой лимит не сгорает."""
    await db_conn.execute(
        "UPDATE spread_history SET clarify_count = COALESCE(clarify_count, 0) + 1 WHERE id = ?",
        (spread_id,),
    )
    await db_conn.commit()


async def repeat_available(spread_id: int) -> bool:
    """Можно ли ещё раз бесплатно переспросить то же самое другими словами.

    Бесплатные повторы защищают от двойного списания за один и тот же вопрос,
    но без лимита один оплаченный расклад превращался в неограниченную серию
    ответов: достаточно было слегка менять формулировку в течение суток.
    """
    async with db_conn.execute(
        "SELECT COALESCE(repeat_count, 0) FROM spread_history WHERE id = ?", (spread_id,)
    ) as cursor:
        row = await cursor.fetchone()
    return (row[0] if row else 0) < config.FREE_REPEATS_PER_SPREAD


async def mark_repeat_used(spread_id: int) -> None:
    """Отмечается после доставленного ответа: сбой генерации повтор не тратит."""
    await db_conn.execute(
        "UPDATE spread_history SET repeat_count = COALESCE(repeat_count, 0) + 1 WHERE id = ?",
        (spread_id,),
    )
    await db_conn.commit()


# --- БЕСПЛАТНАЯ КАРТА ДНЯ ---
async def free_card_available(user_id: int) -> bool:
    """Одна «Карта дня» в сутки — бесплатно и без списания свечей."""
    if not config.FREE_CARD_OF_DAY_DAILY:
        return False
    async with db_conn.execute(
        "SELECT last_card_date FROM user_limits WHERE user_id = ?", (user_id,)
    ) as cursor:
        row = await cursor.fetchone()
    return not row or row[0] != date.today().isoformat()


async def mark_free_card_used(user_id: int) -> None:
    """Ставится после выдачи расклада: за несостоявшуюся карту день не сгорает."""
    today = date.today().isoformat()
    await db_conn.execute(
        """INSERT INTO user_limits (user_id, last_card_date) VALUES (?, ?)
           ON CONFLICT(user_id) DO UPDATE SET last_card_date = ?""",
        (user_id, today, today),
    )
    await db_conn.commit()


# Дневник листается теми же страницами, что и лента админки.
DIARY_PAGE_SIZE = 5


async def count_user_spreads(user_id: int) -> int:
    async with db_conn.execute(
            "SELECT COUNT(*) FROM spread_history WHERE user_id = ?", (user_id,)) as cursor:
        return (await cursor.fetchone())[0]


async def get_user_spreads_history(user_id: int, limit: int = DIARY_PAGE_SIZE, offset: int = 0) -> list[dict]:
    async with db_conn.execute(
            "SELECT id, spread_type, question, cards_text, created_at FROM spread_history WHERE user_id = ? ORDER BY id DESC LIMIT ? OFFSET ?",
            (user_id, limit, offset)) as cursor:
        return [{"id": r[0], "spread_type": r[1], "question": r[2], "cards_text": r[3], "created_at": r[4]} for r in
                await cursor.fetchall()]


# --- ЖУРНАЛ ОБРАЩЕНИЙ (ИСТОЧНИК ДЛЯ АДМИНКИ) ---
async def log_request(
    user_id: int,
    kind: str,
    question: str,
    status: str = "ok",
    note: str = "",
    spread_id: int | None = None,
) -> int:
    """Пишет любое обращение клиента, включая отклонённые и сорвавшиеся.

    status: ok — ответ выдан, rejected — отсеян фильтром, error — сбой генерации.
    """
    cursor = await db_conn.execute(
        """INSERT INTO request_log (user_id, kind, question, status, note, spread_id)
           VALUES (?, ?, ?, ?, ?, ?)""",
        (user_id, kind, question, status, note, spread_id),
    )
    await db_conn.commit()
    return cursor.lastrowid


async def attach_spread_to_request(request_id: int, spread_id: int) -> None:
    """Связывает запись журнала с готовым раскладом, чтобы админ видел ответ."""
    await db_conn.execute(
        "UPDATE request_log SET spread_id = ?, status = 'ok' WHERE id = ?",
        (spread_id, request_id),
    )
    await db_conn.commit()


async def mark_request_status(request_id: int, status: str, note: str = "") -> None:
    await db_conn.execute(
        "UPDATE request_log SET status = ?, note = ? WHERE id = ?", (status, note, request_id)
    )
    await db_conn.commit()


# --- АДМИН: ПРОСМОТР ЗАПРОСОВ ПОЛЬЗОВАТЕЛЕЙ ---
ADMIN_PAGE_SIZE = 5


def _build_request_filter(user_id: int | None, search: str | None) -> tuple[str, list]:
    """Собирает WHERE-часть и параметры. Значения всегда идут плейсхолдерами."""
    clauses, params = [], []
    if user_id is not None:
        clauses.append("user_id = ?")
        params.append(user_id)
    if search:
        clauses.append("question LIKE ?")
        params.append(f"%{search}%")
    return (" WHERE " + " AND ".join(clauses)) if clauses else "", params


async def count_requests(user_id: int | None = None, search: str | None = None) -> int:
    """Считает обращения с учётом фильтров (для пагинации)."""
    where, params = _build_request_filter(user_id, search)
    async with db_conn.execute(f"SELECT COUNT(*) FROM request_log{where}", params) as cursor:
        return (await cursor.fetchone())[0]


async def get_requests_page(
    page: int = 0,
    user_id: int | None = None,
    search: str | None = None,
    page_size: int = ADMIN_PAGE_SIZE,
) -> list[dict]:
    """Страница обращений от новых к старым, опционально по пользователю или подстроке."""
    where, params = _build_request_filter(user_id, search)
    async with db_conn.execute(
        f"""SELECT id, user_id, kind, question, status, note, spread_id, created_at
            FROM request_log{where}
            ORDER BY id DESC LIMIT ? OFFSET ?""",
        (*params, page_size, page * page_size),
    ) as cursor:
        return [
            {"id": r[0], "user_id": r[1], "kind": r[2], "question": r[3], "status": r[4],
             "note": r[5], "spread_id": r[6], "created_at": r[7]}
            for r in await cursor.fetchall()
        ]


async def get_request_by_id(request_id: int) -> dict | None:
    async with db_conn.execute(
        """SELECT id, user_id, kind, question, status, note, spread_id, created_at
           FROM request_log WHERE id = ?""",
        (request_id,),
    ) as cursor:
        row = await cursor.fetchone()
    if not row:
        return None
    return {"id": row[0], "user_id": row[1], "kind": row[2], "question": row[3], "status": row[4],
            "note": row[5], "spread_id": row[6], "created_at": row[7]}


async def get_admin_stats() -> dict:
    """Сводка по боту: пользователи, расклады, активность."""
    stats: dict = {}
    async with db_conn.execute("SELECT COUNT(*) FROM users") as cur:
        stats["users"] = (await cur.fetchone())[0]
    async with db_conn.execute("SELECT COUNT(*), COUNT(DISTINCT user_id) FROM spread_history") as cur:
        row = await cur.fetchone()
        stats["spreads"], stats["active_users"] = row[0], row[1]
    async with db_conn.execute(
        "SELECT COUNT(*) FROM spread_history WHERE date(created_at) = date('now')"
    ) as cur:
        stats["today"] = (await cur.fetchone())[0]
    async with db_conn.execute(
        "SELECT COUNT(*) FROM spread_history WHERE created_at >= datetime('now', '-7 days')"
    ) as cur:
        stats["week"] = (await cur.fetchone())[0]
    async with db_conn.execute(
        "SELECT spread_type, COUNT(*) c FROM spread_history GROUP BY spread_type ORDER BY c DESC"
    ) as cur:
        stats["by_type"] = [(r[0], r[1]) for r in await cur.fetchall()]
    async with db_conn.execute(
        """SELECT user_id, COUNT(*) c, MAX(created_at)
           FROM spread_history GROUP BY user_id ORDER BY c DESC LIMIT 5"""
    ) as cur:
        stats["top_users"] = [(r[0], r[1], r[2]) for r in await cur.fetchall()]

    # Обращения всего и сколько из них отсеял фильтр — видно, не режет ли он живых людей.
    async with db_conn.execute(
        """SELECT COUNT(*),
                  SUM(CASE WHEN status = 'rejected' THEN 1 ELSE 0 END),
                  SUM(CASE WHEN status = 'error' THEN 1 ELSE 0 END)
           FROM request_log"""
    ) as cur:
        total, rejected, failed = await cur.fetchone()
    stats["requests"] = total or 0
    stats["rejected"] = rejected or 0
    stats["failed"] = failed or 0

    # --- ВОРОНКА ---
    # Стартовавшие считаются по balances: строка появляется при выдаче
    # приветственного бонуса, то есть ровно на /start. Таблица users
    # наполняется позже — только когда человек указал дату рождения.
    async with db_conn.execute("SELECT COUNT(*) FROM balances") as cur:
        started = (await cur.fetchone())[0]
    # Именно заполненная анкета, а не наличие строки: строка в users может
    # появиться и от отметки блокировки, и от переключателя напоминаний.
    async with db_conn.execute(
        "SELECT COUNT(*) FROM users WHERE birth_date IS NOT NULL AND birth_date != ''"
    ) as cur:
        profiled = (await cur.fetchone())[0]
    async with db_conn.execute(
        "SELECT COUNT(DISTINCT user_id) FROM payments WHERE refunded = 0"
    ) as cur:
        paying = (await cur.fetchone())[0]
    async with db_conn.execute(
        "SELECT COUNT(*) FROM users WHERE COALESCE(is_blocked, 0) = 1"
    ) as cur:
        blocked_users = (await cur.fetchone())[0]

    stats["funnel"] = {
        "started": started,
        "profiled": profiled,
        "first_spread": stats["active_users"],
        "paying": paying,
        "blocked": blocked_users,
    }

    # Средний чек считается по валютам раздельно: звёзды и рубли не складываются.
    async with db_conn.execute(
        """SELECT currency, COALESCE(AVG(amount), 0), COUNT(*)
           FROM payments WHERE refunded = 0 GROUP BY currency"""
    ) as cur:
        stats["avg_check"] = {r[0]: (r[1], r[2]) for r in await cur.fetchall()}
    return stats


async def get_user_card(user_id: int) -> dict | None:
    """Карточка пользователя для админа: профиль + активность."""
    async with db_conn.execute(
        "SELECT birth_date, birth_arcana FROM users WHERE user_id = ?", (user_id,)
    ) as cur:
        row = await cur.fetchone()

    async with db_conn.execute(
        """SELECT COUNT(*), MIN(created_at), MAX(created_at)
           FROM spread_history WHERE user_id = ?""",
        (user_id,),
    ) as cur:
        cnt, first, last = await cur.fetchone()

    if not row and not cnt:
        return None
    return {
        "user_id": user_id,
        "birth_date": row[0] if row else None,
        "birth_arcana": row[1] if row else None,
        "spreads": cnt,
        "first_seen": first,
        "last_seen": last,
    }


async def get_all_user_ids() -> list[int]:
    """ID всех, кто хоть раз взаимодействовал с ботом (профиль или расклад).

    Заблокировавшие бота исключены: слать им бесполезно, а каждая попытка —
    это лишний запрос к Telegram и минус к скорости рассылки.
    """
    async with db_conn.execute(
        """SELECT user_id FROM users WHERE COALESCE(is_blocked, 0) = 0
           UNION
           SELECT DISTINCT user_id FROM spread_history
           WHERE user_id NOT IN (SELECT user_id FROM users WHERE COALESCE(is_blocked, 0) = 1)"""
    ) as cursor:
        return [r[0] for r in await cursor.fetchall()]


async def mark_blocked(user_id: int, blocked: bool = True) -> None:
    """Отмечает, что бот заблокирован пользователем (или снова разблокирован).

    Снятие отметки — только UPDATE: вызывается оно на каждом /start, и вставка
    плодила бы пустые строки профиля для всех, кто до анкеты не дошёл.
    """
    if blocked:
        await db_conn.execute(
            """INSERT INTO users (user_id, is_blocked) VALUES (?, 1)
               ON CONFLICT(user_id) DO UPDATE SET is_blocked = 1""",
            (user_id,),
        )
    else:
        await db_conn.execute(
            "UPDATE users SET is_blocked = 0 WHERE user_id = ? AND COALESCE(is_blocked, 0) = 1",
            (user_id,),
        )
    await db_conn.commit()


# --- НАПОМИНАНИЕ О БЕСПЛАТНОЙ КАРТЕ ДНЯ ---
async def get_reminder_enabled(user_id: int) -> bool:
    async with db_conn.execute(
        "SELECT COALESCE(daily_reminder, 0) FROM users WHERE user_id = ?", (user_id,)
    ) as cursor:
        row = await cursor.fetchone()
    return bool(row and row[0])


async def set_reminder_enabled(user_id: int, enabled: bool) -> None:
    await db_conn.execute(
        """INSERT INTO users (user_id, daily_reminder) VALUES (?, ?)
           ON CONFLICT(user_id) DO UPDATE SET daily_reminder = ?""",
        (user_id, int(enabled), int(enabled)),
    )
    await db_conn.commit()


async def get_reminder_recipients() -> list[int]:
    """Кому сегодня уместно напомнить о бесплатной карте.

    Отсекаются трое: выключившие напоминания, заблокировавшие бота и те, кто
    карту сегодня уже взял — напоминать им не о чем.
    """
    today = date.today().isoformat()
    async with db_conn.execute(
        """SELECT u.user_id FROM users u
           LEFT JOIN user_limits l ON l.user_id = u.user_id
           WHERE COALESCE(u.daily_reminder, 0) = 1
             AND COALESCE(u.is_blocked, 0) = 0
             AND COALESCE(u.last_reminded, '') != ?
             AND COALESCE(l.last_card_date, '') != ?""",
        (today, today),
    ) as cursor:
        return [r[0] for r in await cursor.fetchall()]


async def mark_reminded(user_id: int) -> None:
    await db_conn.execute(
        "UPDATE users SET last_reminded = ? WHERE user_id = ?",
        (date.today().isoformat(), user_id),
    )
    await db_conn.commit()


# Таблицы с пользовательскими данными. knowledge_chunks сознательно исключена:
# это статичная база знаний на ~190 МБ, восстанавливаемая из CSV (import_chunks.py),
# а Telegram не примет от бота файл больше 50 МБ.
#
# Денежные таблицы обязаны быть здесь: balances — это оплаченный людьми товар,
# payments.charge_id — единственный способ вернуть звёзды через Telegram,
# а transactions — доказательство в любом споре о списании. Бэкап без них
# означает, что после потери базы вернуть деньги технически нечем.
BACKUP_TABLES = (
    "users",
    "user_limits",
    "user_dialogs",
    "photo_cache",
    "spread_history",
    "request_log",
    "balances",
    "transactions",
    "payments",
    "referrals",
    "settings",
    "topup_packages",
)


def create_backup_sync(dest_path: str, include_embeddings: bool = False) -> dict:
    """Создаёт компактную копию пользовательских данных в отдельный SQLite-файл.

    Синхронная: вызывать через asyncio.to_thread, чтобы не блокировать бота.
    """
    import os
    import sqlite3

    if os.path.exists(dest_path):
        os.remove(dest_path)

    src = sqlite3.connect(config.DB_NAME)
    dst = sqlite3.connect(dest_path)
    counts: dict[str, int] = {}
    try:
        src.row_factory = sqlite3.Row
        for table in BACKUP_TABLES:
            schema_row = src.execute(
                "SELECT sql FROM sqlite_master WHERE type='table' AND name=?", (table,)
            ).fetchone()
            if not schema_row or not schema_row[0]:
                continue

            rows = src.execute(f"SELECT * FROM {table}").fetchall()
            columns = [d[0] for d in src.execute(f"SELECT * FROM {table} LIMIT 0").description]

            # Эмбеддинги вопросов — по 3072 числа на запись; в бэкапе они не нужны,
            # дедупликация просто перестроится на новых вопросах.
            drop = set()
            if not include_embeddings and table == "spread_history":
                drop = {"question_embedding"}

            keep = [c for c in columns if c not in drop]
            dst.execute(schema_row[0])
            if rows:
                placeholders = ",".join("?" * len(keep))
                dst.executemany(
                    f"INSERT INTO {table} ({','.join(keep)}) VALUES ({placeholders})",
                    [tuple(r[c] for c in keep) for r in rows],
                )
            counts[table] = len(rows)
        dst.commit()
    finally:
        src.close()
        dst.close()

    counts["_bytes"] = os.path.getsize(dest_path)
    return counts


# --- НАСТРОЙКИ (РЕДАКТИРУЮТСЯ ИЗ АДМИНКИ) ---
SETTINGS_CACHE: dict[str, str] = {}


async def _seed_defaults() -> None:
    """Заполняет настройки и пакеты пополнения при первом запуске.

    Существующие значения не трогаются: иначе каждый рестарт затирал бы цены,
    выставленные администратором.
    """
    defaults = {
        "welcome_bonus": str(config.DEFAULT_WELCOME_BONUS),
        "referral_bonus": str(config.DEFAULT_REFERRAL_BONUS),
        **{f"price_{k}": str(v) for k, v in config.DEFAULT_PRICES.items()},
    }
    for key, value in defaults.items():
        await db_conn.execute(
            "INSERT OR IGNORE INTO settings (key, value) VALUES (?, ?)", (key, value)
        )

    # Уточнение стало бесплатным — старая цена осталась бы висеть в админке.
    await db_conn.execute("DELETE FROM settings WHERE key = 'price_clarification'")

    async with db_conn.execute("SELECT COUNT(*) FROM topup_packages") as cursor:
        if (await cursor.fetchone())[0] == 0:
            for i, p in enumerate(config.DEFAULT_TOPUP_PACKAGES):
                await db_conn.execute(
                    """INSERT INTO topup_packages (title, candles, stars, rub, sort_order)
                       VALUES (?, ?, ?, ?, ?)""",
                    (p["title"], p["candles"], p["stars"], p["rub"], i),
                )
    await db_conn.commit()
    await refresh_settings()


async def refresh_settings() -> None:
    """Перечитывает настройки в кэш: они читаются на каждом запросе."""
    SETTINGS_CACHE.clear()
    async with db_conn.execute("SELECT key, value FROM settings") as cursor:
        async for key, value in cursor:
            SETTINGS_CACHE[key] = value


def get_setting_int(key: str, default: int = 0) -> int:
    try:
        return int(SETTINGS_CACHE.get(key, default))
    except (TypeError, ValueError):
        return default


async def set_setting(key: str, value) -> None:
    await db_conn.execute(
        """INSERT INTO settings (key, value, updated_at) VALUES (?, ?, CURRENT_TIMESTAMP)
           ON CONFLICT(key) DO UPDATE SET value = ?, updated_at = CURRENT_TIMESTAMP""",
        (key, str(value), str(value)),
    )
    await db_conn.commit()
    SETTINGS_CACHE[key] = str(value)


def get_price(spread_type: str) -> int:
    """Цена запроса в свечах. Неизвестный тип не должен стать бесплатным."""
    return get_setting_int(f"price_{spread_type}", config.DEFAULT_PRICES.get(spread_type, 10))


def get_all_prices() -> dict[str, int]:
    return {k: get_price(k) for k in config.DEFAULT_PRICES}


# --- БАЛАНС ---
async def get_balance(user_id: int) -> int:
    async with db_conn.execute("SELECT candles FROM balances WHERE user_id = ?", (user_id,)) as cursor:
        row = await cursor.fetchone()
        return row[0] if row else 0


async def _write_balance(user_id: int, amount: int, kind: str, note: str = "") -> int:
    """Меняет баланс и пишет запись в журнал. Возвращает новый баланс."""
    await db_conn.execute(
        """INSERT INTO balances (user_id, candles) VALUES (?, ?)
           ON CONFLICT(user_id) DO UPDATE SET
               candles = candles + ?, updated_at = CURRENT_TIMESTAMP""",
        (user_id, max(0, amount), amount),
    )
    new_balance = await get_balance(user_id)
    await db_conn.execute(
        """INSERT INTO transactions (user_id, amount, balance_after, kind, note)
           VALUES (?, ?, ?, ?, ?)""",
        (user_id, amount, new_balance, kind, note),
    )
    await db_conn.commit()
    return new_balance


async def add_candles(user_id: int, amount: int, kind: str = "topup", note: str = "") -> int:
    """Начисляет свечи. Возвращает новый баланс."""
    if amount <= 0:
        return await get_balance(user_id)
    new_balance = await _write_balance(user_id, amount, kind, note)
    logger.info(f"💰 [Баланс] {user_id} +{amount} ({kind}) -> {new_balance}")
    return new_balance


async def try_charge(user_id: int, amount: int, note: str = "") -> tuple[bool, int]:
    """Списывает свечи, если их хватает. Возвращает (успех, текущий баланс).

    Проверка и списание идут одним UPDATE с условием: между отдельными
    «проверить» и «списать» параллельный запрос мог бы увести баланс в минус.
    """
    if amount <= 0:
        return True, await get_balance(user_id)

    cursor = await db_conn.execute(
        """UPDATE balances SET candles = candles - ?, updated_at = CURRENT_TIMESTAMP
           WHERE user_id = ? AND candles >= ?""",
        (amount, user_id, amount),
    )
    if cursor.rowcount == 0:
        await db_conn.commit()
        return False, await get_balance(user_id)

    new_balance = await get_balance(user_id)
    await db_conn.execute(
        """INSERT INTO transactions (user_id, amount, balance_after, kind, note)
           VALUES (?, ?, ?, 'charge', ?)""",
        (user_id, -amount, new_balance, note),
    )
    await db_conn.commit()
    logger.info(f"💸 [Баланс] {user_id} -{amount} ({note}) -> {new_balance}")
    return True, new_balance


async def refund_candles(user_id: int, amount: int, note: str = "") -> int:
    """Возврат свечей, если запрос не удался после списания."""
    return await _write_balance(user_id, amount, "refund", note)


async def grant_welcome_bonus(user_id: int) -> int:
    """Разовый стартовый бонус новому пользователю. Повторно не выдаётся."""
    async with db_conn.execute(
        "SELECT welcome_given FROM balances WHERE user_id = ?", (user_id,)
    ) as cursor:
        row = await cursor.fetchone()
    if row and row[0]:
        return 0

    bonus = get_setting_int("welcome_bonus", config.DEFAULT_WELCOME_BONUS)
    if bonus <= 0:
        return 0

    await db_conn.execute(
        """INSERT INTO balances (user_id, candles, welcome_given) VALUES (?, 0, 0)
           ON CONFLICT(user_id) DO NOTHING""",
        (user_id,),
    )
    await db_conn.execute("UPDATE balances SET welcome_given = 1 WHERE user_id = ?", (user_id,))
    await _write_balance(user_id, bonus, "welcome", "Приветственный бонус")
    logger.info(f"🎁 [Баланс] {user_id} получил стартовые {bonus} свечей.")
    return bonus


async def get_transactions(user_id: int, limit: int = 10) -> list[dict]:
    async with db_conn.execute(
        """SELECT amount, balance_after, kind, note, created_at
           FROM transactions WHERE user_id = ? ORDER BY id DESC LIMIT ?""",
        (user_id, limit),
    ) as cursor:
        return [
            {"amount": r[0], "balance_after": r[1], "kind": r[2], "note": r[3], "created_at": r[4]}
            for r in await cursor.fetchall()
        ]


# --- ПАКЕТЫ ПОПОЛНЕНИЯ ---
async def get_topup_packages(only_active: bool = True) -> list[dict]:
    where = "WHERE active = 1" if only_active else ""
    async with db_conn.execute(
        f"""SELECT id, title, candles, stars, rub, active
            FROM topup_packages {where} ORDER BY sort_order, id"""
    ) as cursor:
        return [
            {"id": r[0], "title": r[1], "candles": r[2], "stars": r[3], "rub": r[4], "active": bool(r[5])}
            for r in await cursor.fetchall()
        ]


async def get_topup_package(package_id: int) -> dict | None:
    async with db_conn.execute(
        "SELECT id, title, candles, stars, rub, active FROM topup_packages WHERE id = ?",
        (package_id,),
    ) as cursor:
        row = await cursor.fetchone()
    if not row:
        return None
    return {"id": row[0], "title": row[1], "candles": row[2], "stars": row[3],
            "rub": row[4], "active": bool(row[5])}


async def update_package(package_id: int, **fields) -> None:
    allowed = {"title", "candles", "stars", "rub", "active", "sort_order"}
    updates = {k: v for k, v in fields.items() if k in allowed}
    if not updates:
        return
    assignments = ", ".join(f"{k} = ?" for k in updates)
    await db_conn.execute(
        f"UPDATE topup_packages SET {assignments} WHERE id = ?", (*updates.values(), package_id)
    )
    await db_conn.commit()


async def record_payment(
    user_id: int, charge_id: str, amount: int, currency: str, candles: int, provider: str
) -> bool:
    """Регистрирует платёж. False, если этот charge_id уже обработан."""
    try:
        await db_conn.execute(
            """INSERT INTO payments (user_id, charge_id, amount, currency, candles, provider)
               VALUES (?, ?, ?, ?, ?, ?)""",
            (user_id, charge_id, amount, currency, candles, provider),
        )
        await db_conn.commit()
        return True
    except aiosqlite.IntegrityError:
        logger.warning(f"⚠️ [Оплата] Повторный charge_id {charge_id} — начисление пропущено.")
        return False


def _payment_row(r) -> dict:
    return {"id": r[0], "user_id": r[1], "charge_id": r[2], "amount": r[3], "currency": r[4],
            "candles": r[5], "provider": r[6], "refunded": bool(r[7]), "created_at": r[8]}


_PAYMENT_COLUMNS = "id, user_id, charge_id, amount, currency, candles, provider, refunded, created_at"


async def get_payment_by_charge(charge_id: str) -> dict | None:
    async with db_conn.execute(
        f"SELECT {_PAYMENT_COLUMNS} FROM payments WHERE charge_id = ?", (charge_id,)
    ) as cursor:
        row = await cursor.fetchone()
    return _payment_row(row) if row else None


async def get_payment_by_id(payment_id: int) -> dict | None:
    async with db_conn.execute(
        f"SELECT {_PAYMENT_COLUMNS} FROM payments WHERE id = ?", (payment_id,)
    ) as cursor:
        row = await cursor.fetchone()
    return _payment_row(row) if row else None


async def get_last_payments(user_id: int | None = None, limit: int = 10) -> list[dict]:
    where, params = ("WHERE user_id = ?", [user_id]) if user_id else ("", [])
    async with db_conn.execute(
        f"SELECT {_PAYMENT_COLUMNS} FROM payments {where} ORDER BY id DESC LIMIT ?",
        (*params, limit),
    ) as cursor:
        return [_payment_row(r) for r in await cursor.fetchall()]


async def mark_refunded(charge_id: str) -> None:
    await db_conn.execute("UPDATE payments SET refunded = 1 WHERE charge_id = ?", (charge_id,))
    await db_conn.commit()


async def get_revenue_stats() -> dict:
    """Доход по валютам: звёзды и деньги провайдера считаются раздельно."""
    stats: dict = {}
    async with db_conn.execute(
        """SELECT currency, COALESCE(SUM(amount), 0), COUNT(*)
           FROM payments WHERE refunded = 0 GROUP BY currency"""
    ) as cur:
        stats["by_currency"] = {r[0]: {"amount": r[1], "count": r[2]} for r in await cur.fetchall()}

    async with db_conn.execute(
        """SELECT currency, COALESCE(SUM(amount), 0) FROM payments
           WHERE refunded = 0 AND created_at >= datetime('now', '-30 days') GROUP BY currency"""
    ) as cur:
        stats["month_by_currency"] = {r[0]: r[1] for r in await cur.fetchall()}

    async with db_conn.execute(
        "SELECT COALESCE(SUM(candles), 0), COUNT(*) FROM payments WHERE refunded = 0"
    ) as cur:
        sold, count = await cur.fetchone()
    stats["candles_sold"], stats["payments"] = sold, count

    async with db_conn.execute(
        "SELECT currency, COALESCE(SUM(amount), 0) FROM payments WHERE refunded = 1 GROUP BY currency"
    ) as cur:
        stats["refunded_by_currency"] = {r[0]: r[1] for r in await cur.fetchall()}

    async with db_conn.execute("SELECT COALESCE(SUM(candles), 0) FROM balances") as cur:
        stats["candles_in_wallets"] = (await cur.fetchone())[0]

    async with db_conn.execute(
        "SELECT COALESCE(SUM(-amount), 0) FROM transactions WHERE kind = 'charge'"
    ) as cur:
        stats["candles_spent"] = (await cur.fetchone())[0]
    return stats


# --- РЕФЕРАЛЬНАЯ ПРОГРАММА ---
async def register_referral(user_id: int, inviter_id: int) -> bool:
    """Фиксирует, кто пригласил пользователя. False — если связь недопустима или уже есть."""
    if user_id == inviter_id:
        return False  # самоприглашение

    # Приглашать может только тот, кто сам уже пользовался ботом.
    async with db_conn.execute(
        """SELECT 1 FROM users WHERE user_id = ?
           UNION SELECT 1 FROM spread_history WHERE user_id = ? LIMIT 1""",
        (inviter_id, inviter_id),
    ) as cursor:
        if not await cursor.fetchone():
            return False

    # Уже знакомый пользователь не может задним числом стать чьим-то рефералом.
    async with db_conn.execute("SELECT 1 FROM users WHERE user_id = ?", (user_id,)) as cursor:
        if await cursor.fetchone():
            return False

    try:
        await db_conn.execute(
            "INSERT INTO referrals (user_id, inviter_id) VALUES (?, ?)", (user_id, inviter_id)
        )
        await db_conn.commit()
        return True
    except aiosqlite.IntegrityError:
        return False  # приглашение уже было засчитано другому


async def claim_referral_reward(user_id: int) -> tuple[int, int] | None:
    """Начисляет свечи пригласившему после первого расклада приглашённого.

    Награда привязана к действию, а не к переходу по ссылке: иначе бонусы
    накручивались бы пустыми аккаунтами. Возвращает (ID пригласившего, свечи).
    """
    async with db_conn.execute(
        "SELECT inviter_id, rewarded FROM referrals WHERE user_id = ?", (user_id,)
    ) as cursor:
        row = await cursor.fetchone()
    if not row or row[1]:
        return None

    inviter_id = row[0]
    bonus = get_setting_int("referral_bonus", config.DEFAULT_REFERRAL_BONUS)
    if bonus <= 0:
        return None

    await db_conn.execute("UPDATE referrals SET rewarded = 1 WHERE user_id = ?", (user_id,))
    await db_conn.commit()
    await add_candles(inviter_id, bonus, kind="referral", note=f"Друг {user_id} сделал расклад")
    logger.info(f"🎁 [Реферал] {inviter_id} получил {bonus} свечей за {user_id}.")
    return inviter_id, bonus


async def get_referral_stats(user_id: int) -> dict:
    async with db_conn.execute(
        "SELECT COUNT(*), COALESCE(SUM(rewarded), 0) FROM referrals WHERE inviter_id = ?", (user_id,)
    ) as cursor:
        invited, rewarded = await cursor.fetchone()
    return {"invited": invited, "rewarded": rewarded}


def is_admin_user(user_id: int) -> bool:
    """Администратор пользуется ботом бесплатно — с него свечи не списываются."""
    return config.ADMIN_ID != 0 and user_id == config.ADMIN_ID


# --- НУМЕРОЛОГИЯ И УТИЛИТЫ ---
def calculate_birth_arcana(date_str: str) -> str:
    digits = [int(d) for d in date_str if d.isdigit()]
    total = sum(digits)
    if total > 22:
        total = sum(int(d) for d in str(total))
    if total == 22:
        total = 0
    mapping = {
        1: "Маг (I)", 2: "Верховная Жрица (II)", 3: "Императрица (III)", 4: "Император (IV)",
        5: "Иерофант (V)", 6: "Влюбленные (VI)", 7: "Колесница (VII)", 8: "Сила (VIII)",
        9: "Отшельник (IX)", 10: "Колесо Фортуны (X)", 11: "Справедливость (XI)", 12: "Повешенный (XII)",
        13: "Смерть (XIII)", 14: "Умеренность (XIV)", 15: "Дьявол (XV)", 16: "Башня (XVI)",
        17: "Звезда (XVII)", 18: "Луна (XVIII)", 19: "Солнце (XIX)", 20: "Суд (XX)",
        21: "Мир (XXI)", 0: "Дурак (0)"
    }
    return mapping.get(total, "Неизвестный Аркан")


def calculate_personal_year_arcana(date_str: str, year: int = config.CURRENT_YEAR) -> str:
    try:
        dt = datetime.strptime(date_str, "%d.%m.%Y")
        return calculate_birth_arcana(f"{dt.day:02d}.{dt.month:02d}.{year}")
    except Exception:
        return "Неизвестный Аркан"


def calculate_compatibility_arcana(date1: str, date2: str) -> str:
    digits1 = sum(int(d) for d in date1 if d.isdigit())
    digits2 = sum(int(d) for d in date2 if d.isdigit())
    total = digits1 + digits2
    if total > 22:
        total = sum(int(d) for d in str(total))
    if total == 22:
        total = 0
    return calculate_birth_arcana(f"{total}")


async def get_user_birth_date(user_id: int) -> str | None:
    async with db_conn.execute("SELECT birth_date FROM users WHERE user_id = ?", (user_id,)) as cursor:
        row = await cursor.fetchone()
        return row[0] if row else None


async def get_user_arcana(user_id: int) -> str | None:
    async with db_conn.execute("SELECT birth_arcana FROM users WHERE user_id = ?", (user_id,)) as cursor:
        row = await cursor.fetchone()
        return row[0] if row else None


async def get_user_gender(user_id: int) -> str:
    """'m' | 'f' | 'x'. Неизвестный пол — это 'x', а не женский по умолчанию."""
    async with db_conn.execute("SELECT gender FROM users WHERE user_id = ?", (user_id,)) as cursor:
        row = await cursor.fetchone()
    return row[0] if row and row[0] in ("m", "f") else "x"


async def set_user_gender(user_id: int, gender: str) -> None:
    if gender not in ("m", "f", "x"):
        return
    await db_conn.execute(
        """INSERT INTO users (user_id, gender) VALUES (?, ?)
           ON CONFLICT(user_id) DO UPDATE SET gender = ?""",
        (user_id, gender, gender),
    )
    await db_conn.commit()


def gender_hint(gender: str) -> str:
    """Строка для промпта: явно задаёт род обращения к клиенту."""
    return config.GENDER_HINTS.get(gender, config.GENDER_HINTS["x"])


def cosine_similarity(v1: list[float], v2: list[float]) -> float:
    if v1 is None or v2 is None or len(v1) == 0 or len(v2) == 0:
        return 0.0
    a = np.asarray(v1, dtype=np.float32)
    b = np.asarray(v2, dtype=np.float32)
    if a.shape != b.shape:
        return 0.0
    norm_a = np.linalg.norm(a)
    norm_b = np.linalg.norm(b)
    if not norm_a or not norm_b:
        return 0.0
    return float(np.dot(a, b) / (norm_a * norm_b))


def search_knowledge_base_sync(query_vector: list[float], limit: int = config.RAG_CHUNKS_LIMIT) -> str:
    """Выполняет RAG-поиск в памяти по уже вычисленному вектору запроса.

    Все близости считаются одним матрично-векторным произведением по заранее
    нормированной матрице, а топ-N отбирается через argpartition без полной сортировки.
    """
    if KNOWLEDGE_MATRIX is None or not query_vector:
        return ""
    try:
        q = np.asarray(query_vector, dtype=np.float32)
        q_norm = np.linalg.norm(q)
        if q_norm == 0:
            return ""
        sims = KNOWLEDGE_MATRIX @ (q / q_norm)

        k = min(limit, sims.shape[0])
        top_idx = np.argpartition(-sims, k - 1)[:k]
        top_idx = top_idx[np.argsort(-sims[top_idx])]

        top = [
            f"📖 [{KNOWLEDGE_META[i][0]}]\n{KNOWLEDGE_META[i][1]}"
            for i in top_idx
            if sims[i] >= config.RAG_MIN_SIMILARITY
        ]
        return "\n\n---\n\n".join(top) if top else ""
    except Exception:
        logger.warning("⚠️ [RAG] Ошибка поиска по базе знаний.", exc_info=True)
        return ""