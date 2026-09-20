import aiosqlite
import hashlib
import json
import logging
import os
from datetime import datetime, timezone
from typing import Any, Optional

from config import config

logger = logging.getLogger(__name__)


class Database:
    """
    Асинхронный слой работы с базой данных SQLite через aiosqlite.
    Поддерживает управление пользователями, чатами, группами и кэширование расписания.
    """

    def __init__(self, db_path: Optional[str] = None):
        self.db_path = db_path or os.getenv("DATABASE_PATH", config.DATABASE_PATH)
        db_dir = os.path.dirname(self.db_path)
        if db_dir:
            os.makedirs(db_dir, exist_ok=True)

    async def init_db(self) -> None:
        """
        Инициализирует таблицы базы данных в строгом соответствии с ТЗ.
        """
        async with aiosqlite.connect(self.db_path) as db:
            await db.execute("PRAGMA journal_mode=WAL;")
            await db.execute("PRAGMA synchronous=NORMAL;")
            await db.execute("PRAGMA busy_timeout=10000;")
            await db.execute("PRAGMA temp_store=MEMORY;")
            await db.execute("PRAGMA cache_size=-64000;")
            await db.execute("PRAGMA foreign_keys=ON;")

            # 1. Таблица users
            await db.execute("""
                CREATE TABLE IF NOT EXISTS users (
                    user_id INTEGER PRIMARY KEY,
                    group_id TEXT,
                    group_url TEXT,
                    group_name TEXT,
                    notifications_enabled BOOLEAN DEFAULT 1,
                    username TEXT,
                    first_name TEXT,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    last_active_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                );
            """)

            # Безопасная миграция существующих баз данных (добавление новых колонок)
            async with db.execute("PRAGMA table_info(users);") as cursor:
                columns = {row[1] for row in await cursor.fetchall()}
            if "username" not in columns:
                await db.execute("ALTER TABLE users ADD COLUMN username TEXT;")
            if "first_name" not in columns:
                await db.execute("ALTER TABLE users ADD COLUMN first_name TEXT;")
            if "created_at" not in columns:
                await db.execute("ALTER TABLE users ADD COLUMN created_at TIMESTAMP;")
                await db.execute("UPDATE users SET created_at = CURRENT_TIMESTAMP WHERE created_at IS NULL;")
            if "last_active_at" not in columns:
                await db.execute("ALTER TABLE users ADD COLUMN last_active_at TIMESTAMP;")
                await db.execute("UPDATE users SET last_active_at = CURRENT_TIMESTAMP WHERE last_active_at IS NULL;")

            if "language" not in columns:
                await db.execute("ALTER TABLE users ADD COLUMN language TEXT DEFAULT 'ru';")
            if "notify_lead_minutes" not in columns:
                await db.execute("ALTER TABLE users ADD COLUMN notify_lead_minutes INTEGER DEFAULT 0;")
            if "evening_notify_time" not in columns:
                await db.execute("ALTER TABLE users ADD COLUMN evening_notify_time TEXT DEFAULT '20:00';")
            if "morning_notify_time" not in columns:
                await db.execute("ALTER TABLE users ADD COLUMN morning_notify_time TEXT DEFAULT '08:00';")
            if "subgroup" not in columns:
                await db.execute("ALTER TABLE users ADD COLUMN subgroup INTEGER DEFAULT 0;")
            if "subgroup_overrides" not in columns:
                await db.execute("ALTER TABLE users ADD COLUMN subgroup_overrides TEXT DEFAULT '{}';")

            # Таблица пропущенных пар (сон / отметка 'не иду на пару')
            await db.execute("""
                CREATE TABLE IF NOT EXISTS skipped_pairs (
                    user_id INTEGER NOT NULL,
                    date TEXT NOT NULL,
                    pair_number INTEGER NOT NULL,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    PRIMARY KEY (user_id, date, pair_number)
                );
            """)
            await db.execute("CREATE INDEX IF NOT EXISTS idx_skipped_pairs_user_date ON skipped_pairs(user_id, date);")

            # 2. Таблица chats (поддержка групп и тем/подразделов супергрупп)
            await db.execute("""
                CREATE TABLE IF NOT EXISTS chats (
                    chat_id INTEGER NOT NULL,
                    message_thread_id INTEGER,
                    group_id TEXT,
                    group_url TEXT,
                    notifications_enabled BOOLEAN DEFAULT 1,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    language TEXT DEFAULT 'ru',
                    notify_lead_minutes INTEGER DEFAULT 0,
                    evening_notify_time TEXT DEFAULT '20:00',
                    morning_notify_time TEXT DEFAULT '08:00'
                );
            """)
            await db.execute("CREATE UNIQUE INDEX IF NOT EXISTS idx_chats_chat_thread ON chats(chat_id, ifnull(message_thread_id, 0));")
            await db.execute("CREATE INDEX IF NOT EXISTS idx_chats_group_id ON chats(group_id);")

            # Безопасная миграция существующих баз данных для chats
            async with db.execute("PRAGMA table_info(chats);") as cursor:
                chat_columns = {row[1] for row in await cursor.fetchall()}
            if "language" not in chat_columns:
                await db.execute("ALTER TABLE chats ADD COLUMN language TEXT DEFAULT 'ru';")
            if "notify_lead_minutes" not in chat_columns:
                await db.execute("ALTER TABLE chats ADD COLUMN notify_lead_minutes INTEGER DEFAULT 0;")
            if "evening_notify_time" not in chat_columns:
                await db.execute("ALTER TABLE chats ADD COLUMN evening_notify_time TEXT DEFAULT '20:00';")
            if "morning_notify_time" not in chat_columns:
                await db.execute("ALTER TABLE chats ADD COLUMN morning_notify_time TEXT DEFAULT '08:00';")

            # 3. Таблица groups
            await db.execute("""
                CREATE TABLE IF NOT EXISTS groups (
                    id TEXT PRIMARY KEY,
                    specialty_name TEXT NOT NULL,
                    group_name TEXT NOT NULL,
                    relative_url TEXT NOT NULL
                );
            """)

            # Индексы для быстрого поиска по специальности и названию группы
            await db.execute("CREATE INDEX IF NOT EXISTS idx_groups_specialty ON groups(specialty_name);")
            await db.execute("CREATE INDEX IF NOT EXISTS idx_groups_name ON groups(group_name);")

            # 4. Таблица timetable_cache
            await db.execute("""
                CREATE TABLE IF NOT EXISTS timetable_cache (
                    group_id TEXT NOT NULL,
                    date TEXT NOT NULL,
                    data_json TEXT NOT NULL,
                    hash TEXT,
                    image_file_id TEXT,
                    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    PRIMARY KEY (group_id, date)
                );
            """)

            # Безопасная миграция timetable_cache (добавление hash и image_file_id, если их нет)
            async with db.execute("PRAGMA table_info(timetable_cache);") as cursor:
                tc_columns = {row[1] for row in await cursor.fetchall()}
            if "hash" not in tc_columns:
                await db.execute("ALTER TABLE timetable_cache ADD COLUMN hash TEXT;")
            if "image_file_id" not in tc_columns:
                await db.execute("ALTER TABLE timetable_cache ADD COLUMN image_file_id TEXT;")

            # 5. Таблица starostas (старосты групп)
            await db.execute("""
                CREATE TABLE IF NOT EXISTS starostas (
                    group_id TEXT PRIMARY KEY,
                    user_id INTEGER NOT NULL,
                    username TEXT,
                    full_name TEXT,
                    appointed_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                );
            """)

            # 6. Таблица homework (домашние задания)
            await db.execute("""
                CREATE TABLE IF NOT EXISTS homework (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    group_id TEXT NOT NULL,
                    subject TEXT NOT NULL,
                    due_date TEXT NOT NULL,
                    task_text TEXT NOT NULL,
                    author_id INTEGER NOT NULL,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                );
            """)
            await db.execute("CREATE INDEX IF NOT EXISTS idx_hw_group_date ON homework(group_id, due_date);")

            # 7. Таблица starosta_requests (заявки на старосту / сложение полномочий)
            await db.execute("""
                CREATE TABLE IF NOT EXISTS starosta_requests (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    group_id TEXT NOT NULL,
                    user_id INTEGER NOT NULL,
                    username TEXT,
                    full_name TEXT,
                    request_type TEXT DEFAULT 'claim',
                    status TEXT DEFAULT 'pending',
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    resolved_at TIMESTAMP
                );
            """)
            await db.execute("CREATE INDEX IF NOT EXISTS idx_st_req_group_status ON starosta_requests(group_id, status);")

            # 8. Таблица group_chat_links (ссылка на беседу учебной группы)
            await db.execute("""
                CREATE TABLE IF NOT EXISTS group_chat_links (
                    group_id TEXT PRIMARY KEY,
                    chat_link TEXT NOT NULL,
                    updated_by INTEGER,
                    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                );
            """)

            # 9. Таблица user_notes (личные заметки и дедлайны студента)
            await db.execute("""
                CREATE TABLE IF NOT EXISTS user_notes (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    user_id INTEGER NOT NULL,
                    note_text TEXT NOT NULL,
                    target_date TEXT,
                    target_pair INTEGER,
                    reminded BOOLEAN DEFAULT 0,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                );
            """)
            await db.execute("CREATE INDEX IF NOT EXISTS idx_user_notes_uid ON user_notes(user_id);")

            # Безопасная миграция user_notes
            async with db.execute("PRAGMA table_info(user_notes);") as cursor:
                un_columns = {row[1] for row in await cursor.fetchall()}
            if "target_date" not in un_columns:
                await db.execute("ALTER TABLE user_notes ADD COLUMN target_date TEXT;")
            if "target_pair" not in un_columns:
                await db.execute("ALTER TABLE user_notes ADD COLUMN target_pair INTEGER;")
            if "reminded" not in un_columns:
                await db.execute("ALTER TABLE user_notes ADD COLUMN reminded BOOLEAN DEFAULT 0;")
            await db.execute("CREATE INDEX IF NOT EXISTS idx_user_notes_date_rem ON user_notes(target_date, reminded);")

            # 10. Таблица group_exams (экзамены, зачеты и консультации группы)
            await db.execute("""
                CREATE TABLE IF NOT EXISTS group_exams (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    group_id TEXT NOT NULL,
                    subject TEXT NOT NULL,
                    exam_date TEXT NOT NULL,
                    exam_time TEXT,
                    room TEXT,
                    teacher TEXT,
                    author_id INTEGER NOT NULL,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                );
            """)
            await db.execute("CREATE INDEX IF NOT EXISTS idx_group_exams ON group_exams(group_id, exam_date);")

            # 11. Таблица group_deputies (заместители старост групп - максимум 1 на группу)
            await db.execute("""
                CREATE TABLE IF NOT EXISTS group_deputies (
                    group_id TEXT PRIMARY KEY,
                    user_id INTEGER NOT NULL,
                    username TEXT,
                    full_name TEXT,
                    appointed_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                );
            """)
            await db.execute("CREATE INDEX IF NOT EXISTS idx_deputies_user ON group_deputies(user_id);")

            await db.commit()
            logger.info("База данных SQLite успешно инициализирована (%s)", self.db_path)

    # -------------------------------------------------------------------------
    # РАБОТА С ПОЛЬЗОВАТЕЛЯМИ (users)
    # -------------------------------------------------------------------------

    async def get_user(self, user_id: int) -> Optional[dict[str, Any]]:
        """
        Возвращает данные пользователя по user_id.
        """
        async with aiosqlite.connect(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            async with db.execute(
                "SELECT * FROM users WHERE user_id = ?",
                (user_id,)
            ) as cursor:
                row = await cursor.fetchone()
                if row:
                    data = dict(row)
                    data["notifications_enabled"] = bool(row["notifications_enabled"])
                    data["language"] = row["language"] or "ru"
                    data["notify_lead_minutes"] = int(row["notify_lead_minutes"] or 0)
                    data["evening_notify_time"] = row["evening_notify_time"] or "20:00"
                    data["morning_notify_time"] = row["morning_notify_time"] or "08:00"
                    data["subgroup"] = int(row["subgroup"] or 0) if "subgroup" in row.keys() else 0
                    raw_overrides = row["subgroup_overrides"] if "subgroup_overrides" in row.keys() else "{}"
                    try:
                        data["subgroup_overrides"] = json.loads(raw_overrides) if raw_overrides else {}
                    except Exception:
                        data["subgroup_overrides"] = {}
                    return data
                return None

    async def set_user_subgroup(
        self, user_id: int, subgroup: int, overrides: Optional[dict[str, int]] = None
    ) -> None:
        """
        Устанавливает выбранную подгруппу пользователя (0 = обе, 1 = первая, 2 = вторая)
        и опциональные персональные переопределения по предметам.
        """
        overrides_json = json.dumps(overrides or {}, ensure_ascii=False)
        async with aiosqlite.connect(self.db_path) as db:
            await db.execute("""
                UPDATE users
                SET subgroup = ?, subgroup_overrides = ?
                WHERE user_id = ?
            """, (subgroup, overrides_json, user_id))
            await db.commit()

    async def get_user_subgroup(self, user_id: int) -> tuple[int, dict[str, int]]:
        """
        Возвращает (subgroup, subgroup_overrides_dict) для пользователя.
        """
        async with aiosqlite.connect(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            async with db.execute(
                "SELECT subgroup, subgroup_overrides FROM users WHERE user_id = ?", (user_id,)
            ) as cursor:
                row = await cursor.fetchone()
                if not row:
                    return 0, {}
                sub = int(row["subgroup"] or 0) if "subgroup" in row.keys() else 0
                overrides_raw = row["subgroup_overrides"] if "subgroup_overrides" in row.keys() else "{}"
                try:
                    overrides = json.loads(overrides_raw) if overrides_raw else {}
                except Exception:
                    overrides = {}
                return sub, overrides

    async def toggle_skipped_pair(self, user_id: int, date_str: str, pair_number: int) -> bool:
        """
        Переключает статус пропуска пары (сон/пропуск):
        Если пара уже пропущена — удаляет и возвращает False.
        Если еще не пропущена — добавляет и возвращает True.
        """
        async with aiosqlite.connect(self.db_path) as db:
            async with db.execute("""
                SELECT 1 FROM skipped_pairs WHERE user_id = ? AND date = ? AND pair_number = ?
            """, (user_id, date_str, pair_number)) as cursor:
                exists = await cursor.fetchone()

            if exists:
                await db.execute("""
                    DELETE FROM skipped_pairs WHERE user_id = ? AND date = ? AND pair_number = ?
                """, (user_id, date_str, pair_number))
                await db.commit()
                return False
            else:
                await db.execute("""
                    INSERT OR REPLACE INTO skipped_pairs (user_id, date, pair_number)
                    VALUES (?, ?, ?)
                """, (user_id, date_str, pair_number))
                await db.commit()
                return True

    async def get_skipped_pairs(self, user_id: int, date_str: str) -> list[int]:
        """
        Возвращает список номеров пар, которые пользователь отметил как пропущенные на указанную дату.
        """
        async with aiosqlite.connect(self.db_path) as db:
            async with db.execute("""
                SELECT pair_number FROM skipped_pairs WHERE user_id = ? AND date = ? ORDER BY pair_number
            """, (user_id, date_str)) as cursor:
                rows = await cursor.fetchall()
                return [int(r[0]) for r in rows]

    async def is_pair_skipped(self, user_id: int, date_str: str, pair_number: int) -> bool:
        """
        Проверяет, отметил ли пользователь конкретную пару как пропущенную.
        """
        async with aiosqlite.connect(self.db_path) as db:
            async with db.execute("""
                SELECT 1 FROM skipped_pairs WHERE user_id = ? AND date = ? AND pair_number = ?
            """, (user_id, date_str, pair_number)) as cursor:
                row = await cursor.fetchone()
                return bool(row)

    async def clear_skipped_pairs(self, user_id: int, date_str: str) -> None:
        """
        Сбрасывает все пропуски пар пользователя на указанную дату.
        """
        async with aiosqlite.connect(self.db_path) as db:
            await db.execute("""
                DELETE FROM skipped_pairs WHERE user_id = ? AND date = ?
            """, (user_id, date_str))
            await db.commit()

    async def get_user_by_username(self, username: str) -> Optional[dict[str, Any]]:
        """Находит пользователя по @username (без учета регистра и символа @)."""
        clean_username = username.lstrip("@").strip().lower()
        if not clean_username:
            return None
        async with aiosqlite.connect(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            async with db.execute(
                "SELECT * FROM users WHERE LOWER(username) = ? LIMIT 1;",
                (clean_username,)
            ) as cur:
                row = await cur.fetchone()
                return dict(row) if row else None

    async def upsert_user(
        self,
        user_id: int,
        group_id: str,
        group_url: str,
        group_name: str,
        notifications_enabled: bool = True,
        username: Optional[str] = None,
        first_name: Optional[str] = None,
    ) -> None:
        """
        Создает или обновляет данные пользователя (выбор группы и профиль).
        """
        async with aiosqlite.connect(self.db_path) as db:
            await db.execute("""
                INSERT INTO users (user_id, group_id, group_url, group_name, notifications_enabled, username, first_name, last_active_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, CURRENT_TIMESTAMP)
                ON CONFLICT(user_id) DO UPDATE SET
                    group_id = excluded.group_id,
                    group_url = excluded.group_url,
                    group_name = excluded.group_name,
                    username = COALESCE(excluded.username, users.username),
                    first_name = COALESCE(excluded.first_name, users.first_name),
                    last_active_at = CURRENT_TIMESTAMP;
            """, (user_id, group_id, group_url, group_name, int(notifications_enabled), username, first_name))
            await db.commit()
            logger.info("Пользователь %d привязан к группе %s (%s)", user_id, group_name, group_id)

    async def touch_user(
        self,
        user_id: int,
        username: Optional[str] = None,
        first_name: Optional[str] = None,
    ) -> None:
        """
        Обновляет время активности и профиль пользователя при взаимодействии.
        """
        async with aiosqlite.connect(self.db_path) as db:
            await db.execute("""
                INSERT INTO users (user_id, username, first_name, last_active_at)
                VALUES (?, ?, ?, CURRENT_TIMESTAMP)
                ON CONFLICT(user_id) DO UPDATE SET
                    username = COALESCE(excluded.username, users.username),
                    first_name = COALESCE(excluded.first_name, users.first_name),
                    last_active_at = CURRENT_TIMESTAMP;
            """, (user_id, username, first_name))
            await db.commit()

    async def get_admin_stats(self) -> dict[str, int]:
        """
        Возвращает общую статистику пользователей и групповых чатов для админ-панели.
        """
        async with aiosqlite.connect(self.db_path) as db:
            async with db.execute("SELECT COUNT(*) FROM users;") as cur:
                total_users = (await cur.fetchone())[0]
            async with db.execute("SELECT COUNT(*) FROM users WHERE user_id > 0;") as cur:
                private_users = (await cur.fetchone())[0]
            async with db.execute("SELECT COUNT(*) FROM users WHERE user_id < 0;") as cur:
                legacy_chats = (await cur.fetchone())[0]
            async with db.execute("SELECT COUNT(*) FROM chats;") as cur:
                chats_count = (await cur.fetchone())[0]
            group_chats = legacy_chats + chats_count
            async with db.execute("SELECT COUNT(*) FROM users WHERE group_id IS NOT NULL AND group_id != '';") as cur:
                with_group = (await cur.fetchone())[0]
            async with db.execute("SELECT COUNT(*) FROM users WHERE notifications_enabled = 1;") as cur:
                notifications_on = (await cur.fetchone())[0]
            async with db.execute("SELECT COUNT(*) FROM groups;") as cur:
                total_groups = (await cur.fetchone())[0]
            return {
                "total_users": total_users,
                "private_users": private_users,
                "group_chats": group_chats,
                "with_group": with_group,
                "notifications_on": notifications_on,
                "total_groups": total_groups,
            }

    async def get_top_groups(self, limit: int = 15) -> list[dict[str, Any]]:
        """
        Возвращает топ групп по количеству зарегистрированных студентов.
        """
        async with aiosqlite.connect(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            async with db.execute("""
                SELECT group_name, COUNT(*) as count
                FROM users
                WHERE group_name IS NOT NULL AND group_name != ''
                GROUP BY group_name
                ORDER BY count DESC, group_name ASC
                LIMIT ?;
            """, (limit,)) as cur:
                rows = await cur.fetchall()
                return [{"group_name": r["group_name"], "count": r["count"]} for r in rows]

    async def get_recent_users(self, limit: int = 20) -> list[dict[str, Any]]:
        """
        Возвращает список последних пользователей бота с их данными.
        """
        async with aiosqlite.connect(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            async with db.execute("""
                SELECT user_id, username, first_name, group_name, notifications_enabled, created_at, last_active_at
                FROM users
                ORDER BY created_at DESC, user_id DESC
                LIMIT ?;
            """, (limit,)) as cur:
                rows = await cur.fetchall()
                return [dict(r) for r in rows]

    async def get_all_user_ids(self) -> list[int]:
        """
        Возвращает список ID всех пользователей для массовой рассылки.
        """
        async with aiosqlite.connect(self.db_path) as db:
            async with db.execute("SELECT user_id FROM users;") as cur:
                rows = await cur.fetchall()
                return [r[0] for r in rows]

    async def get_active_group_ids(self) -> list[str]:
        """
        Возвращает список уникальных group_id выбранных студентами групп и чатами/темами.
        """
        async with aiosqlite.connect(self.db_path) as db:
            async with db.execute("""
                SELECT DISTINCT group_id FROM (
                    SELECT group_id FROM users WHERE group_id IS NOT NULL AND group_id != ''
                    UNION
                    SELECT group_id FROM chats WHERE group_id IS NOT NULL AND group_id != ''
                );
            """) as cur:
                rows = await cur.fetchall()
                return [r[0] for r in rows]

    async def clear_timetable_cache(self) -> int:
        """
        Очищает все записи в таблице timetable_cache.
        """
        async with aiosqlite.connect(self.db_path) as db:
            async with db.execute("DELETE FROM timetable_cache;") as cur:
                deleted = cur.rowcount
            await db.commit()
            return deleted

    async def set_user_notifications(self, user_id: int, enabled: bool) -> None:
        """
        Устанавливает статус уведомлений для пользователя (например, при блокировке бота).
        """
        async with aiosqlite.connect(self.db_path) as db:
            await db.execute(
                "UPDATE users SET notifications_enabled = ? WHERE user_id = ?",
                (int(enabled), user_id)
            )
            await db.commit()
            logger.info("Уведомления для пользователя %d установлены в %s", user_id, enabled)

    async def toggle_notifications(self, user_id: int) -> bool:
        """
        Инвертирует статус уведомлений пользователя и возвращает новое значение.
        """
        async with aiosqlite.connect(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            async with db.execute("SELECT notifications_enabled FROM users WHERE user_id = ?", (user_id,)) as cursor:
                row = await cursor.fetchone()
                current = bool(row["notifications_enabled"]) if row else True
            new_status = not current
            await db.execute(
                "UPDATE users SET notifications_enabled = ? WHERE user_id = ?",
                (int(new_status), user_id)
            )
            await db.commit()
            return new_status

    async def get_active_users_grouped_by_group(self) -> dict[str, list[int]]:
        """
        Возвращает словарь {group_id: [user_id_1, user_id_2, ...]}
        для всех пользователей с включенными уведомлениями.
        Позволяет группировать рассылку, делая ровно 1 парсинг на группу.
        """
        grouped: dict[str, list[int]] = {}
        async with aiosqlite.connect(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            async with db.execute(
                "SELECT user_id, group_id FROM users WHERE notifications_enabled = 1 AND group_id IS NOT NULL"
            ) as cursor:
                rows = await cursor.fetchall()
                for row in rows:
                    gid = row["group_id"]
                    uid = row["user_id"]
                    grouped.setdefault(gid, []).append(uid)
        return grouped

    # -------------------------------------------------------------------------
    # РАБОТА С ЧАТАМИ И ТЕМАМИ СУПЕРГРУПП (chats)
    # -------------------------------------------------------------------------

    async def upsert_chat(
        self,
        chat_id: int,
        message_thread_id: Optional[int],
        group_id: str,
        group_url: str,
        notifications_enabled: bool = True,
        group_name: Optional[str] = None,
        **kwargs: Any,
    ) -> None:
        """
        Создает или обновляет настройки группы/темы форума для группового чата.
        """
        async with aiosqlite.connect(self.db_path) as db:
            async with db.execute(
                "SELECT 1 FROM chats WHERE chat_id = ? AND ((message_thread_id IS NULL AND ? IS NULL) OR message_thread_id = ?)",
                (chat_id, message_thread_id, message_thread_id)
            ) as cur:
                exists = await cur.fetchone()
            if exists:
                await db.execute("""
                    UPDATE chats
                    SET group_id = ?, group_url = ?, notifications_enabled = ?
                    WHERE chat_id = ? AND ((message_thread_id IS NULL AND ? IS NULL) OR message_thread_id = ?)
                """, (group_id, group_url, int(notifications_enabled), chat_id, message_thread_id, message_thread_id))
            else:
                await db.execute("""
                    INSERT INTO chats (chat_id, message_thread_id, group_id, group_url, notifications_enabled, created_at)
                    VALUES (?, ?, ?, ?, ?, CURRENT_TIMESTAMP)
                """, (chat_id, message_thread_id, group_id, group_url, int(notifications_enabled)))
            await db.commit()
            logger.info("Чат %d (thread %s) привязан к группе %s", chat_id, message_thread_id, group_id)

    async def get_chat(
        self, chat_id: int, message_thread_id: Optional[int] = None
    ) -> Optional[dict[str, Any]]:
        """
        Возвращает данные чата или темы форума по chat_id и message_thread_id с подтягиванием group_name.
        """
        async with aiosqlite.connect(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            async with db.execute("""
                SELECT c.chat_id, c.message_thread_id, c.group_id, c.group_url, c.notifications_enabled, c.created_at, g.group_name, c.language, c.notify_lead_minutes, c.evening_notify_time, c.morning_notify_time
                FROM chats c
                LEFT JOIN groups g ON c.group_id = g.id
                WHERE c.chat_id = ? AND ((c.message_thread_id IS NULL AND ? IS NULL) OR c.message_thread_id = ?)
            """, (chat_id, message_thread_id, message_thread_id)) as cur:
                row = await cur.fetchone()
                if row:
                    return {
                        "chat_id": row["chat_id"],
                        "message_thread_id": row["message_thread_id"],
                        "group_id": row["group_id"],
                        "group_url": row["group_url"],
                        "group_name": row["group_name"] or row["group_id"],
                        "notifications_enabled": bool(row["notifications_enabled"]),
                        "created_at": row["created_at"],
                        "language": row["language"] or "ru",
                        "notify_lead_minutes": int(row["notify_lead_minutes"] or 0),
                        "evening_notify_time": row["evening_notify_time"] or "20:00",
                        "morning_notify_time": row["morning_notify_time"] or "08:00",
                    }
                return None

    async def get_active_chats_grouped_by_group(self) -> dict[str, list[tuple[int, Optional[int]]]]:
        """
        Возвращает словарь {group_id: [(chat_id, message_thread_id), ...]}
        для всех чатов и тем форума с включенными уведомлениями.
        """
        grouped: dict[str, list[tuple[int, Optional[int]]]] = {}
        async with aiosqlite.connect(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            async with db.execute(
                "SELECT chat_id, message_thread_id, group_id FROM chats WHERE notifications_enabled = 1 AND group_id IS NOT NULL AND group_id != ''"
            ) as cursor:
                rows = await cursor.fetchall()
                for row in rows:
                    gid = row["group_id"]
                    cid = row["chat_id"]
                    mtid = row["message_thread_id"]
                    grouped.setdefault(gid, []).append((cid, mtid))
        return grouped

    async def set_chat_notifications(self, chat_id: int, message_thread_id: Optional[int], enabled: bool) -> None:
        """Устанавливает статус уведомлений для чата или темы."""
        async with aiosqlite.connect(self.db_path) as db:
            await db.execute("""
                UPDATE chats SET notifications_enabled = ?
                WHERE chat_id = ? AND ((message_thread_id IS NULL AND ? IS NULL) OR message_thread_id = ?)
            """, (int(enabled), chat_id, message_thread_id, message_thread_id))
            await db.commit()

    async def delete_chat(self, chat_id: int, message_thread_id: Optional[int] = None) -> None:
        """Удаляет запись о чате или теме из БД при исключении бота."""
        async with aiosqlite.connect(self.db_path) as db:
            if message_thread_id is not None:
                await db.execute("DELETE FROM chats WHERE chat_id = ? AND message_thread_id = ?", (chat_id, message_thread_id))
            else:
                await db.execute("DELETE FROM chats WHERE chat_id = ?", (chat_id,))
            await db.commit()

    async def get_all_chats(self) -> list[dict[str, Any]]:
        """Возвращает список всех зарегистрированных чатов и тем."""
        async with aiosqlite.connect(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            async with db.execute("""
                SELECT c.chat_id, c.message_thread_id, c.group_id, c.group_url, c.notifications_enabled, c.created_at, g.group_name
                FROM chats c
                LEFT JOIN groups g ON c.group_id = g.id
                ORDER BY c.created_at DESC
            """) as cur:
                rows = await cur.fetchall()
                return [dict(r) for r in rows]

    # -------------------------------------------------------------------------
    # РАБОТА С ГРУППАМИ (groups)
    # -------------------------------------------------------------------------

    async def upsert_groups_bulk(self, groups_data: list[dict[str, str]]) -> None:
        """
        Пакетное добавление или обновление групп.
        Формат элемента: {"id": str, "specialty_name": str, "group_name": str, "relative_url": str}
        """
        if not groups_data:
            return

        async with aiosqlite.connect(self.db_path) as db:
            await db.executemany("""
                INSERT INTO groups (id, specialty_name, group_name, relative_url)
                VALUES (:id, :specialty_name, :group_name, :relative_url)
                ON CONFLICT(id) DO UPDATE SET
                    specialty_name = excluded.specialty_name,
                    group_name = excluded.group_name,
                    relative_url = excluded.relative_url;
            """, groups_data)
            await db.commit()
            logger.info("Сохранено/обновлено групп в БД: %d", len(groups_data))

    async def get_all_specialties(self) -> list[str]:
        """
        Возвращает отсортированный список уникальных специальностей.
        """
        async with aiosqlite.connect(self.db_path) as db:
            async with db.execute(
                "SELECT DISTINCT specialty_name FROM groups ORDER BY specialty_name ASC"
            ) as cursor:
                rows = await cursor.fetchall()
                return [row[0] for row in rows]

    async def get_groups_by_specialty(self, specialty_name: str) -> list[dict[str, str]]:
        """
        Возвращает список всех групп выбранной специальности.
        """
        async with aiosqlite.connect(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            async with db.execute(
                "SELECT id, specialty_name, group_name, relative_url FROM groups WHERE specialty_name = ? ORDER BY group_name ASC",
                (specialty_name,)
            ) as cursor:
                rows = await cursor.fetchall()
                return [dict(row) for row in rows]

    async def get_group_by_id(self, group_id: str) -> Optional[dict[str, str]]:
        """
        Возвращает информацию о группе по её идентификатору.
        """
        async with aiosqlite.connect(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            async with db.execute(
                "SELECT id, specialty_name, group_name, relative_url FROM groups WHERE id = ?",
                (group_id,)
            ) as cursor:
                row = await cursor.fetchone()
                return dict(row) if row else None

    async def count_groups(self) -> int:
        """
        Возвращает общее число групп в базе данных.
        """
        async with aiosqlite.connect(self.db_path) as db:
            async with db.execute("SELECT COUNT(*) FROM groups") as cursor:
                row = await cursor.fetchone()
                return row[0] if row else 0

    # -------------------------------------------------------------------------
    # РАБОТА С КЭШЕМ РАСПИСАНИЯ (timetable_cache)
    # -------------------------------------------------------------------------

    async def get_cached_timetable(self, group_id: str, date_str: str) -> Optional[dict[str, Any]]:
        """
        Получает расписание из кэша, если оно есть и не устарело (в пределах CACHE_TTL_HOURS).
        """
        async with aiosqlite.connect(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            async with db.execute("""
                SELECT data_json, updated_at
                FROM timetable_cache
                WHERE group_id = ? AND date = ?
            """, (group_id, date_str)) as cursor:
                row = await cursor.fetchone()
                if not row:
                    return None

                try:
                    updated_at = datetime.fromisoformat(row["updated_at"])
                    # Приводим к UTC/локальному времени для расчета разницы
                    if updated_at.tzinfo is None:
                        updated_at = updated_at.replace(tzinfo=timezone.utc)
                    age_hours = (datetime.now(timezone.utc) - updated_at).total_seconds() / 3600
                    if age_hours > config.CACHE_TTL_HOURS:
                        logger.debug("Кэш для %s на %s устарел (возраст: %.1f ч)", group_id, date_str, age_hours)
                        return None

                    return json.loads(row["data_json"])
                except Exception as e:
                    logger.warning("Ошибка разбора кэша для %s (%s): %s", group_id, date_str, e)
                    return None

    async def save_cached_timetable(
        self, group_id: str, date_str: str, data: dict[str, Any], data_hash: Optional[str] = None
    ) -> None:
        """
        Сохраняет расписание в кэш timetable_cache вместе с хэшем структуры пар.
        """
        data_json = json.dumps(data, ensure_ascii=False)
        if not data_hash:
            lessons = data.get("lessons", [])
            canonical = json.dumps(lessons, sort_keys=True, ensure_ascii=False)
            data_hash = hashlib.md5(canonical.encode("utf-8")).hexdigest()

        now_iso = datetime.now(timezone.utc).isoformat()
        async with aiosqlite.connect(self.db_path) as db:
            await db.execute("""
                INSERT INTO timetable_cache (group_id, date, data_json, hash, updated_at)
                VALUES (?, ?, ?, ?, ?)
                ON CONFLICT(group_id, date) DO UPDATE SET
                    data_json = excluded.data_json,
                    hash = excluded.hash,
                    image_file_id = CASE WHEN timetable_cache.hash != excluded.hash THEN NULL ELSE timetable_cache.image_file_id END,
                    updated_at = excluded.updated_at;
            """, (group_id, date_str, data_json, data_hash, now_iso))
            await db.commit()
            logger.debug("Расписание сохранено в кэш для %s на %s (hash: %s)", group_id, date_str, data_hash[:8])

    async def get_cached_timetable_entry(self, group_id: str, date_str: str) -> Optional[dict[str, Any]]:
        """
        Возвращает полную запись кэша (распарсенные данные, хэш и дату обновления).
        """
        async with aiosqlite.connect(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            async with db.execute("""
                SELECT data_json, hash, updated_at
                FROM timetable_cache
                WHERE group_id = ? AND date = ?
            """, (group_id, date_str)) as cursor:
                row = await cursor.fetchone()
                if not row:
                    return None
                try:
                    return {
                        "data": json.loads(row["data_json"]),
                        "hash": row["hash"],
                        "updated_at": row["updated_at"],
                    }
                except Exception as e:
                    logger.warning("Ошибка чтения записи кэша для %s (%s): %s", group_id, date_str, e)
                    return None

    async def get_cached_schedule_image(self, group_id: str, date_str: str) -> Optional[str]:
        """
        Возвращает сохраненный Telegram file_id картинки расписания для группы и даты,
        если кэш не устарел (в пределах CACHE_TTL_HOURS).
        """
        async with aiosqlite.connect(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            async with db.execute("""
                SELECT image_file_id, updated_at
                FROM timetable_cache
                WHERE group_id = ? AND date = ? AND image_file_id IS NOT NULL AND image_file_id != ''
            """, (group_id, date_str)) as cursor:
                row = await cursor.fetchone()
                if not row or not row["image_file_id"]:
                    return None
                try:
                    updated_at = datetime.fromisoformat(row["updated_at"])
                    if updated_at.tzinfo is None:
                        updated_at = updated_at.replace(tzinfo=timezone.utc)
                    age_hours = (datetime.now(timezone.utc) - updated_at).total_seconds() / 3600
                    if age_hours > config.CACHE_TTL_HOURS:
                        return None
                    return row["image_file_id"]
                except Exception:
                    return row["image_file_id"]

    async def save_cached_schedule_image(self, group_id: str, date_str: str, image_file_id: str) -> None:
        """
        Сохраняет Telegram file_id картинки расписания в кэш для группы и даты.
        """
        now_iso = datetime.now(timezone.utc).isoformat()
        async with aiosqlite.connect(self.db_path) as db:
            await db.execute("""
                INSERT INTO timetable_cache (group_id, date, data_json, image_file_id, updated_at)
                VALUES (?, ?, '{}', ?, ?)
                ON CONFLICT(group_id, date) DO UPDATE SET
                    image_file_id = excluded.image_file_id,
                    updated_at = excluded.updated_at;
            """, (group_id, date_str, image_file_id, now_iso))
            await db.commit()
            logger.debug("Кэш картинки расписания (file_id) сохранен для %s на %s", group_id, date_str)

    async def find_teacher_schedule(
        self,
        teacher_query: str,
        start_date: Optional[str] = None,
        days_ahead: int = 7,
        min_date_str: Optional[str] = None,
    ) -> list[dict[str, Any]]:
        """
        Сканирует сохраненное расписание всех групп в timetable_cache
        и находит все пары указанного преподавателя на ближайшие дни.
        Возвращает структурированный список пар (день, время, академическая группа, кабинет, подгруппа).
        """
        query_lower = teacher_query.strip().lower()
        if not query_lower:
            return []

        from datetime import date as dt_date, timedelta as dt_timedelta
        effective_start = start_date or min_date_str
        if not effective_start:
            start_dt = dt_date.today()
            start_date = start_dt.isoformat()
        else:
            try:
                start_dt = dt_date.fromisoformat(effective_start)
                start_date = effective_start
            except Exception:
                start_dt = dt_date.today()
                start_date = start_dt.isoformat()

        end_dt = start_dt + dt_timedelta(days=days_ahead)
        start_str = start_dt.isoformat()
        end_str = end_dt.isoformat()

        results: list[dict[str, Any]] = []

        async with aiosqlite.connect(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            async with db.execute("""
                SELECT tc.group_id, tc.date, tc.data_json, g.group_name
                FROM timetable_cache tc
                LEFT JOIN groups g ON tc.group_id = g.id
                WHERE tc.date >= ? AND tc.date <= ?
                ORDER BY tc.date ASC
            """, (start_str, end_str)) as cur:
                rows = await cur.fetchall()

            for row in rows:
                date_val = row["date"]
                group_id = row["group_id"]
                group_name = row["group_name"] or group_id
                try:
                    data = json.loads(row["data_json"])
                except Exception:
                    continue

                day_name = data.get("day_name", "")
                lessons = data.get("lessons", [])
                for l in lessons:
                    teacher_name = str(l.get("teacher", "")).strip()
                    if query_lower in teacher_name.lower():
                        results.append({
                            "date": date_val,
                            "day_name": day_name,
                            "pair_number": l.get("pair_number", 1),
                            "time": l.get("time", ""),
                            "start_time": l.get("start_time", ""),
                            "end_time": l.get("end_time", ""),
                            "subject": l.get("subject", "Занятие"),
                            "room": l.get("room", ""),
                            "teacher": teacher_name,
                            "group_id": group_id,
                            "group_name": group_name,
                            "subgroup": l.get("subgroup", 0),
                            "format": l.get("format", "Очно"),
                        })

        results.sort(key=lambda x: (x["date"], x["pair_number"], x["group_name"]))
        return results

    async def get_group_subjects_and_teachers(self, group_id: str) -> dict[str, dict[str, Any]]:
        """
        Сканирует кэшированное расписание группы и собирает уникальные предметы,
        преподавателей, аудитории и ближайшие пары.
        """
        async with aiosqlite.connect(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            async with db.execute("""
                SELECT date, data_json
                FROM timetable_cache
                WHERE group_id = ?
                ORDER BY date ASC
            """, (group_id,)) as cur:
                rows = await cur.fetchall()

        subjects: dict[str, dict[str, Any]] = {}
        for row in rows:
            date_val = row["date"]
            try:
                data = json.loads(row["data_json"])
            except Exception:
                continue

            day_name = data.get("day_name", "")
            lessons = data.get("lessons", [])
            for l in lessons:
                subj = str(l.get("subject", "")).strip()
                if not subj or subj.lower() in ("занятие", "без названия"):
                    continue

                teacher = str(l.get("teacher", "")).strip()
                room = str(l.get("room", "")).strip()
                sub = l.get("subgroup", 0)
                pair_num = l.get("pair_number", 1)
                time_str = l.get("time", "")

                if subj not in subjects:
                    subjects[subj] = {
                        "subject": subj,
                        "teachers": set(),
                        "rooms": set(),
                        "lessons": []
                    }

                if teacher:
                    if sub:
                        subjects[subj]["teachers"].add(f"{teacher} ({sub} подгр.)")
                    else:
                        subjects[subj]["teachers"].add(teacher)

                if room:
                    subjects[subj]["rooms"].add(room)

                # Сохраняем ближайшие даты занятий
                time_part = f" ({time_str})" if time_str else ""
                lesson_info = f"• {day_name}, {date_val}: {pair_num} пара{time_part}"
                if lesson_info not in subjects[subj]["lessons"] and len(subjects[subj]["lessons"]) < 5:
                    subjects[subj]["lessons"].append(lesson_info)

        # Преобразуем множества в отсортированные списки
        for subj in subjects:
            subjects[subj]["teachers"] = sorted(list(subjects[subj]["teachers"]))
            subjects[subj]["rooms"] = sorted(list(subjects[subj]["rooms"]))

        return subjects

    # -------------------------------------------------------------------------
    # ЯЗЫК И ВРЕМЯ НАПОМИНАНИЯ (i18n & Lead notifications)
    # -------------------------------------------------------------------------

    async def get_user_language(self, user_id: int) -> str:
        """Возвращает язык пользователя ('ru' или 'en')."""
        async with aiosqlite.connect(self.db_path) as db:
            async with db.execute("SELECT language FROM users WHERE user_id = ?", (user_id,)) as cur:
                row = await cur.fetchone()
                if row and row[0]:
                    return row[0]
        return "ru"

    async def set_user_language(self, user_id: int, language: str) -> None:
        """Сохраняет выбранный язык пользователя."""
        async with aiosqlite.connect(self.db_path) as db:
            await db.execute("""
                INSERT INTO users (user_id, language, last_active_at)
                VALUES (?, ?, CURRENT_TIMESTAMP)
                ON CONFLICT(user_id) DO UPDATE SET
                    language = excluded.language,
                    last_active_at = CURRENT_TIMESTAMP;
            """, (user_id, language))
            await db.commit()

    async def get_user_lead_minutes(self, user_id: int) -> int:
        """Возвращает за сколько минут до пары отправлять напоминание (0 = в момент начала)."""
        async with aiosqlite.connect(self.db_path) as db:
            async with db.execute("SELECT notify_lead_minutes FROM users WHERE user_id = ?", (user_id,)) as cur:
                row = await cur.fetchone()
                if row and row[0] is not None:
                    return int(row[0])
        return 0

    async def set_user_lead_minutes(self, user_id: int, minutes: int) -> None:
        """Устанавливает за сколько минут до пары отправлять напоминание."""
        async with aiosqlite.connect(self.db_path) as db:
            await db.execute("""
                INSERT INTO users (user_id, notify_lead_minutes, last_active_at)
                VALUES (?, ?, CURRENT_TIMESTAMP)
                ON CONFLICT(user_id) DO UPDATE SET
                    notify_lead_minutes = excluded.notify_lead_minutes,
                    last_active_at = CURRENT_TIMESTAMP;
            """, (user_id, minutes))
            await db.commit()

    async def set_chat_lead_minutes(self, chat_id: int, message_thread_id: Optional[int], minutes: int) -> None:
        """Устанавливает за сколько минут до пары отправлять напоминание в чат/тему."""
        async with aiosqlite.connect(self.db_path) as db:
            await db.execute("""
                UPDATE chats SET notify_lead_minutes = ?
                WHERE chat_id = ? AND ((message_thread_id IS NULL AND ? IS NULL) OR message_thread_id = ?)
            """, (minutes, chat_id, message_thread_id, message_thread_id))
            await db.commit()

    # -------------------------------------------------------------------------
    # СТАРОСТА ГРУППЫ (starostas)
    # -------------------------------------------------------------------------

    async def get_group_starosta(self, group_id: str) -> Optional[dict[str, Any]]:
        """Возвращает данные о старосте группы."""
        async with aiosqlite.connect(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            async with db.execute(
                "SELECT group_id, user_id, username, full_name, appointed_at FROM starostas WHERE group_id = ?",
                (group_id,)
            ) as cur:
                row = await cur.fetchone()
                if row:
                    return dict(row)
        return None

    async def set_group_starosta(
        self, group_id: str, user_id: int, username: Optional[str] = None, full_name: Optional[str] = None
    ) -> None:
        """Назначает старосту группы."""
        async with aiosqlite.connect(self.db_path) as db:
            await db.execute("""
                INSERT INTO starostas (group_id, user_id, username, full_name, appointed_at)
                VALUES (?, ?, ?, ?, CURRENT_TIMESTAMP)
                ON CONFLICT(group_id) DO UPDATE SET
                    user_id = excluded.user_id,
                    username = excluded.username,
                    full_name = excluded.full_name,
                    appointed_at = CURRENT_TIMESTAMP;
            """, (group_id, user_id, username, full_name))
            await db.commit()
            logger.info("Пользователь %d назначен старостой группы %s", user_id, group_id)

    async def remove_group_starosta(self, group_id: str) -> bool:
        """Удаляет старосту группы (сложение полномочий)."""
        async with aiosqlite.connect(self.db_path) as db:
            res = await db.execute("DELETE FROM starostas WHERE group_id = ?", (group_id,))
            await db.commit()
            return res.rowcount > 0

    async def create_starosta_request(
        self,
        group_id: str,
        user_id: int,
        username: Optional[str] = None,
        full_name: Optional[str] = None,
        request_type: str = "claim",
    ) -> int:
        """Создает заявку на статус старосты или сложение полномочий."""
        async with aiosqlite.connect(self.db_path) as db:
            cur = await db.execute("""
                INSERT INTO starosta_requests (group_id, user_id, username, full_name, request_type, status, created_at)
                VALUES (?, ?, ?, ?, ?, 'pending', CURRENT_TIMESTAMP)
            """, (group_id, user_id, username, full_name, request_type))
            await db.commit()
            return cur.lastrowid

    async def get_pending_starosta_request(
        self, group_id: str, user_id: int, request_type: str = "claim"
    ) -> Optional[dict[str, Any]]:
        """Возвращает активную ожидающую заявку пользователя для группы."""
        async with aiosqlite.connect(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            async with db.execute("""
                SELECT * FROM starosta_requests
                WHERE group_id = ? AND user_id = ? AND request_type = ? AND status = 'pending'
                ORDER BY id DESC LIMIT 1
            """, (group_id, user_id, request_type)) as cur:
                row = await cur.fetchone()
                if row:
                    return dict(row)
        return None

    async def get_starosta_request_by_id(self, req_id: int) -> Optional[dict[str, Any]]:
        """Возвращает заявку по ID."""
        async with aiosqlite.connect(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            async with db.execute("SELECT * FROM starosta_requests WHERE id = ?", (req_id,)) as cur:
                row = await cur.fetchone()
                if row:
                    return dict(row)
        return None

    async def update_starosta_request_status(self, req_id: int, status: str) -> bool:
        """Обновляет статус заявки ('approved', 'rejected')."""
        async with aiosqlite.connect(self.db_path) as db:
            res = await db.execute("""
                UPDATE starosta_requests
                SET status = ?, resolved_at = CURRENT_TIMESTAMP
                WHERE id = ?
            """, (status, req_id))
            await db.commit()
            return res.rowcount > 0

    # -------------------------------------------------------------------------
    # ЗАМЕСТИТЕЛИ СТАРОСТ (group_deputies)
    # -------------------------------------------------------------------------

    async def get_group_deputy(self, group_id: str) -> Optional[dict[str, Any]]:
        """Возвращает данные заместителя старосты группы (не более одного на группу)."""
        async with aiosqlite.connect(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            async with db.execute(
                "SELECT group_id, user_id, username, full_name, appointed_at FROM group_deputies WHERE group_id = ?",
                (group_id,)
            ) as cur:
                row = await cur.fetchone()
                return dict(row) if row else None

    async def set_group_deputy(
        self, group_id: str, user_id: int, username: Optional[str] = None, full_name: Optional[str] = None
    ) -> bool:
        """Назначает заместителя старосты (ровно 1 заместитель на группу)."""
        async with aiosqlite.connect(self.db_path) as db:
            await db.execute("""
                INSERT INTO group_deputies (group_id, user_id, username, full_name, appointed_at)
                VALUES (?, ?, ?, ?, CURRENT_TIMESTAMP)
                ON CONFLICT(group_id) DO UPDATE SET
                    user_id = excluded.user_id,
                    username = excluded.username,
                    full_name = excluded.full_name,
                    appointed_at = CURRENT_TIMESTAMP;
            """, (group_id, user_id, username, full_name))
            await db.commit()
            logger.info("Пользователь %d назначен заместителем старосты группы %s", user_id, group_id)
            return True

    async def remove_group_deputy(self, group_id: str) -> bool:
        """Удаляет заместителя старосты группы."""
        async with aiosqlite.connect(self.db_path) as db:
            res = await db.execute("DELETE FROM group_deputies WHERE group_id = ?", (group_id,))
            await db.commit()
            return res.rowcount > 0

    async def is_starosta_or_deputy(self, group_id: str, user_id: int) -> bool:
        """Проверяет, является ли пользователь старостой или заместителем старосты данной группы."""
        async with aiosqlite.connect(self.db_path) as db:
            async with db.execute(
                "SELECT 1 FROM starostas WHERE group_id = ? AND user_id = ?",
                (group_id, user_id)
            ) as cur:
                if await cur.fetchone():
                    return True
            async with db.execute(
                "SELECT 1 FROM group_deputies WHERE group_id = ? AND user_id = ?",
                (group_id, user_id)
            ) as cur:
                if await cur.fetchone():
                    return True
        return False

    async def is_user_starosta_of_group(self, user_id: int, group_id: str) -> bool:
        """Проверяет, имеет ли пользователь права старосты или зама в группе."""
        return await self.is_starosta_or_deputy(group_id, user_id)

    # -------------------------------------------------------------------------
    # ДОМАШНИЕ ЗАДАНИЯ (homework)
    # -------------------------------------------------------------------------

    async def add_homework(
        self, group_id: str, subject: str, due_date: str, task_text: str, author_id: int
    ) -> int:
        """Добавляет домашнее задание и возвращает его ID."""
        async with aiosqlite.connect(self.db_path) as db:
            cur = await db.execute("""
                INSERT INTO homework (group_id, subject, due_date, task_text, author_id, created_at)
                VALUES (?, ?, ?, ?, ?, CURRENT_TIMESTAMP)
            """, (group_id, subject.strip(), due_date.strip(), task_text.strip(), author_id))
            await db.commit()
            logger.info("Добавлено ДЗ id=%d для группы %s по предмету %s на %s", cur.lastrowid, group_id, subject, due_date)
            return cur.lastrowid

    async def get_upcoming_homework(self, group_id: str, limit: int = 15) -> list[dict[str, Any]]:
        """Возвращает список актуальных домашних заданий для группы."""
        async with aiosqlite.connect(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            async with db.execute("""
                SELECT id, group_id, subject, due_date, task_text, author_id, created_at
                FROM homework
                WHERE group_id = ?
                ORDER BY due_date ASC, id DESC
                LIMIT ?
            """, (group_id, limit)) as cur:
                rows = await cur.fetchall()
                return [dict(r) for r in rows]

    async def get_homework_for_date(self, group_id: str, date_str: str) -> list[dict[str, Any]]:
        """Возвращает список домашних заданий на конкретную дату."""
        async with aiosqlite.connect(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            async with db.execute("""
                SELECT id, group_id, subject, due_date, task_text, author_id, created_at
                FROM homework
                WHERE group_id = ? AND (due_date = ? OR due_date LIKE ?)
                ORDER BY id ASC
            """, (group_id, date_str, f"%{date_str}%")) as cur:
                rows = await cur.fetchall()
                return [dict(r) for r in rows]

    async def get_homework_by_id(self, hw_id: int) -> Optional[dict[str, Any]]:
        """Возвращает домашнее задание по ID."""
        async with aiosqlite.connect(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            async with db.execute("SELECT * FROM homework WHERE id = ?", (hw_id,)) as cur:
                row = await cur.fetchone()
                if row:
                    return dict(row)
        return None

    async def delete_homework(self, hw_id: int, group_id: Optional[str] = None) -> bool:
        """Удаляет домашнее задание по ID (с опциональной проверкой группы)."""
        async with aiosqlite.connect(self.db_path) as db:
            if group_id:
                res = await db.execute("DELETE FROM homework WHERE id = ? AND group_id = ?", (hw_id, group_id))
            else:
                res = await db.execute("DELETE FROM homework WHERE id = ?", (hw_id,))
            await db.commit()
            return res.rowcount > 0

    async def get_subscribers_for_group(self, group_id: str) -> list[tuple[int, Optional[int]]]:
        """
        Возвращает список (chat_id, message_thread_id) для всех подписчиков группы
        (пользователи ЛС + чаты/топики групп) для трансляции нового ДЗ.
        """
        subscribers: list[tuple[int, Optional[int]]] = []
        async with aiosqlite.connect(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            async with db.execute(
                "SELECT user_id FROM users WHERE group_id = ? AND notifications_enabled = 1",
                (group_id,)
            ) as cur:
                for r in await cur.fetchall():
                    subscribers.append((r["user_id"], None))

            async with db.execute(
                "SELECT chat_id, message_thread_id FROM chats WHERE group_id = ? AND notifications_enabled = 1",
                (group_id,)
            ) as cur:
                for r in await cur.fetchall():
                    subscribers.append((r["chat_id"], r["message_thread_id"]))
        return subscribers

    async def clean_old_cache(self, days: int = 7) -> None:
        """
        Очищает записи кэша старше указанного количества дней.
        """
        async with aiosqlite.connect(self.db_path) as db:
            await db.execute("""
                DELETE FROM timetable_cache
                WHERE julianday('now') - julianday(updated_at) > ?;
            """, (days,))
            await db.commit()

    # -------------------------------------------------------------------------
    # НАСТРОЙКИ ВРЕМЕНИ РАССЫЛОК (evening_notify_time, morning_notify_time)
    # -------------------------------------------------------------------------

    async def set_user_evening_time(self, user_id: int, time_str: str) -> None:
        """Устанавливает время вечерней рассылки для пользователя ('20:00', 'off' и т.д.)."""
        async with aiosqlite.connect(self.db_path) as db:
            await db.execute("UPDATE users SET evening_notify_time = ? WHERE user_id = ?", (time_str, user_id))
            await db.commit()

    async def set_user_morning_time(self, user_id: int, time_str: str) -> None:
        """Устанавливает время утренней рассылки для пользователя ('08:00', 'off' и т.д.)."""
        async with aiosqlite.connect(self.db_path) as db:
            await db.execute("UPDATE users SET morning_notify_time = ? WHERE user_id = ?", (time_str, user_id))
            await db.commit()

    async def set_chat_evening_time(self, chat_id: int, message_thread_id: Optional[int], time_str: str) -> None:
        """Устанавливает время вечерней рассылки для группы/темы."""
        async with aiosqlite.connect(self.db_path) as db:
            await db.execute("""
                UPDATE chats SET evening_notify_time = ?
                WHERE chat_id = ? AND ((message_thread_id IS NULL AND ? IS NULL) OR message_thread_id = ?)
            """, (time_str, chat_id, message_thread_id, message_thread_id))
            await db.commit()

    async def set_chat_morning_time(self, chat_id: int, message_thread_id: Optional[int], time_str: str) -> None:
        """Устанавливает время утренней рассылки для группы/темы."""
        async with aiosqlite.connect(self.db_path) as db:
            await db.execute("""
                UPDATE chats SET morning_notify_time = ?
                WHERE chat_id = ? AND ((message_thread_id IS NULL AND ? IS NULL) OR message_thread_id = ?)
            """, (time_str, chat_id, message_thread_id, message_thread_id))
            await db.commit()

    async def get_subscribers_for_evening_slot(self, time_str: str) -> dict[str, list[tuple[int, Optional[int]]]]:
        """
        Возвращает {group_id: [(chat_id, message_thread_id), ...]}
        для подписчиков, у которых evening_notify_time совпадает со слотом.
        """
        result: dict[str, list[tuple[int, Optional[int]]]] = {}
        async with aiosqlite.connect(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            # 1. Личные чаты пользователей
            async with db.execute("""
                SELECT user_id, group_id FROM users
                WHERE notifications_enabled = 1 AND group_id IS NOT NULL AND group_id != ''
                  AND (evening_notify_time = ? OR (evening_notify_time IS NULL AND ? = '20:00'))
            """, (time_str, time_str)) as cur:
                for r in await cur.fetchall():
                    result.setdefault(r["group_id"], []).append((r["user_id"], None))

            # 2. Групповые чаты и топики
            async with db.execute("""
                SELECT chat_id, message_thread_id, group_id FROM chats
                WHERE notifications_enabled = 1 AND group_id IS NOT NULL AND group_id != ''
                  AND (evening_notify_time = ? OR (evening_notify_time IS NULL AND ? = '20:00'))
            """, (time_str, time_str)) as cur:
                for r in await cur.fetchall():
                    result.setdefault(r["group_id"], []).append((r["chat_id"], r["message_thread_id"]))
        return result

    async def get_subscribers_for_morning_slot(self, time_str: str) -> dict[str, list[tuple[int, Optional[int]]]]:
        """
        Возвращает {group_id: [(chat_id, message_thread_id), ...]}
        для подписчиков, у которых morning_notify_time совпадает со слотом.
        """
        result: dict[str, list[tuple[int, Optional[int]]]] = {}
        async with aiosqlite.connect(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            # 1. Личные чаты пользователей
            async with db.execute("""
                SELECT user_id, group_id FROM users
                WHERE notifications_enabled = 1 AND group_id IS NOT NULL AND group_id != ''
                  AND (morning_notify_time = ? OR (morning_notify_time IS NULL AND ? = '08:00'))
            """, (time_str, time_str)) as cur:
                for r in await cur.fetchall():
                    result.setdefault(r["group_id"], []).append((r["user_id"], None))

            # 2. Групповые чаты и топики
            async with db.execute("""
                SELECT chat_id, message_thread_id, group_id FROM chats
                WHERE notifications_enabled = 1 AND group_id IS NOT NULL AND group_id != ''
                  AND (morning_notify_time = ? OR (morning_notify_time IS NULL AND ? = '08:00'))
            """, (time_str, time_str)) as cur:
                for r in await cur.fetchall():
                    result.setdefault(r["group_id"], []).append((r["chat_id"], r["message_thread_id"]))
        return result

    # -------------------------------------------------------------------------
    # ССЫЛКИ НА ЧАТ ГРУППЫ (group_chat_links)
    # -------------------------------------------------------------------------

    async def get_group_chat_link(self, group_id: str) -> Optional[dict[str, Any]]:
        """Возвращает ссылку на беседу группы."""
        async with aiosqlite.connect(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            async with db.execute("SELECT * FROM group_chat_links WHERE group_id = ?", (group_id,)) as cur:
                row = await cur.fetchone()
                if row:
                    return dict(row)
        return None

    async def set_group_chat_link(self, group_id: str, chat_link: str, updated_by: int) -> None:
        """Сохраняет или обновляет ссылку на беседу группы."""
        async with aiosqlite.connect(self.db_path) as db:
            await db.execute("""
                INSERT INTO group_chat_links (group_id, chat_link, updated_by, updated_at)
                VALUES (?, ?, ?, CURRENT_TIMESTAMP)
                ON CONFLICT(group_id) DO UPDATE SET
                    chat_link = excluded.chat_link,
                    updated_by = excluded.updated_by,
                    updated_at = CURRENT_TIMESTAMP;
            """, (group_id, chat_link.strip(), updated_by))
            await db.commit()

    async def delete_group_chat_link(self, group_id: str) -> bool:
        """Удаляет ссылку на беседу группы."""
        async with aiosqlite.connect(self.db_path) as db:
            res = await db.execute("DELETE FROM group_chat_links WHERE group_id = ?", (group_id,))
            await db.commit()
            return res.rowcount > 0

    # -------------------------------------------------------------------------
    # ЛИЧНЫЕ ЗАМЕТКИ СТУДЕНТА (user_notes)
    # -------------------------------------------------------------------------

    async def add_user_note(
        self,
        user_id: int,
        note_text: str,
        target_date: Optional[str] = None,
        target_pair: Optional[int] = None,
    ) -> int:
        """Добавляет личную заметку студента с опциональным дедлайном и парой."""
        async with aiosqlite.connect(self.db_path) as db:
            cur = await db.execute("""
                INSERT INTO user_notes (user_id, note_text, target_date, target_pair, reminded, created_at)
                VALUES (?, ?, ?, ?, 0, CURRENT_TIMESTAMP)
            """, (user_id, note_text.strip(), target_date, target_pair))
            await db.commit()
            return cur.lastrowid

    async def get_user_notes(self, user_id: int) -> list[dict[str, Any]]:
        """Возвращает список личных заметок студента."""
        async with aiosqlite.connect(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            async with db.execute("""
                SELECT id, user_id, note_text, target_date, target_pair, reminded, created_at
                FROM user_notes
                WHERE user_id = ?
                ORDER BY id DESC
            """, (user_id,)) as cur:
                rows = await cur.fetchall()
                return [dict(r) for r in rows]

    async def delete_user_note(self, note_id: int, user_id: int) -> bool:
        """Удаляет личную заметку студента."""
        async with aiosqlite.connect(self.db_path) as db:
            res = await db.execute("DELETE FROM user_notes WHERE id = ? AND user_id = ?", (note_id, user_id))
            await db.commit()
            return res.rowcount > 0

    async def get_pending_note_reminders(self, current_date_str: str) -> list[dict[str, Any]]:
        """Возвращает список заметок для напоминания:
        - Заметки с конкретной датой, у которых срок наступил или наступает сегодня.
        - Заметки без даты, но с указанной парой (напоминаем каждый раз в день обучения).
        """
        async with aiosqlite.connect(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            async with db.execute("""
                SELECT id, user_id, note_text, target_date, target_pair, reminded, created_at
                FROM user_notes
                WHERE (reminded = 0 OR reminded IS NULL)
                  AND (
                      -- Заметки с датой: только когда срок наступил
                      (target_date IS NOT NULL AND target_date <= ?)
                      OR
                      -- Заметки без даты, но с парой: напоминаем каждый день
                      (target_date IS NULL AND target_pair IS NOT NULL)
                  )
                ORDER BY target_date ASC NULLS LAST, target_pair ASC
            """, (current_date_str,)) as cur:
                rows = await cur.fetchall()
                return [dict(r) for r in rows]

    async def mark_note_reminded(self, note_id: int) -> bool:
        """Помечает заметку как напомненную."""
        async with aiosqlite.connect(self.db_path) as db:
            res = await db.execute("UPDATE user_notes SET reminded = 1 WHERE id = ?", (note_id,))
            await db.commit()
            return res.rowcount > 0

    async def cleanup_old_reminded_notes(self, days_old: int = 7) -> int:
        """Удаляет напомненные заметки старше N дней. Возвращает количество удаленных."""
        async with aiosqlite.connect(self.db_path) as db:
            res = await db.execute(
                """
                DELETE FROM user_notes
                WHERE reminded = 1
                  AND created_at < datetime('now', ? || ' days')
                """,
                (f"-{days_old}",)
            )
            await db.commit()
            deleted = res.rowcount
            if deleted > 0:
                logger.info("Автоочистка: удалено %d напомненных заметок старше %d дней.", deleted, days_old)
            return deleted


    # -------------------------------------------------------------------------
    # ЭКЗАМЕНЫ И СЕССИЯ (group_exams)
    # -------------------------------------------------------------------------

    async def add_group_exam(
        self,
        group_id: str,
        subject: str,
        exam_date: str,
        exam_time: str = "",
        room: str = "",
        teacher: str = "",
        author_id: int = 0,
    ) -> int:
        """Добавляет экзамен или зачет для группы."""
        async with aiosqlite.connect(self.db_path) as db:
            cur = await db.execute("""
                INSERT INTO group_exams (group_id, subject, exam_date, exam_time, room, teacher, author_id, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, CURRENT_TIMESTAMP)
            """, (group_id, subject.strip(), exam_date.strip(), exam_time.strip(), room.strip(), teacher.strip(), author_id))
            await db.commit()
            return cur.lastrowid

    async def get_group_exams(self, group_id: str) -> list[dict[str, Any]]:
        """Возвращает список экзаменов/зачетов группы, отсортированных по дате."""
        async with aiosqlite.connect(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            async with db.execute("""
                SELECT id, group_id, subject, exam_date, exam_time, room, teacher, author_id, created_at
                FROM group_exams
                WHERE group_id = ?
                ORDER BY exam_date ASC, id ASC
            """, (group_id,)) as cur:
                rows = await cur.fetchall()
                return [dict(r) for r in rows]

    async def delete_group_exam(self, exam_id: int, group_id: Optional[str] = None) -> bool:
        """Удаляет экзамен/зачет."""
        async with aiosqlite.connect(self.db_path) as db:
            if group_id:
                res = await db.execute("DELETE FROM group_exams WHERE id = ? AND group_id = ?", (exam_id, group_id))
            else:
                res = await db.execute("DELETE FROM group_exams WHERE id = ?", (exam_id,))
            await db.commit()
            return res.rowcount > 0

    # -------------------------------------------------------------------------
    # СТАТИСТИКА БОТА (для /stats администратора)
    # -------------------------------------------------------------------------

    async def get_bot_stats(self) -> dict[str, Any]:
        """
        Возвращает агрегированную статистику бота для команды /stats:
        - Общее кол-во пользователей и активных за 7 дней
        - Кол-во групп, старост, групповых чатов
        - Топ-5 групп по числу студентов
        - Кол-во ДЗ и заметок в базе
        """
        async with aiosqlite.connect(self.db_path) as db:
            db.row_factory = aiosqlite.Row

            async with db.execute("SELECT COUNT(*) as cnt FROM users") as cur:
                total_users = (await cur.fetchone())["cnt"]

            async with db.execute(
                "SELECT COUNT(*) as cnt FROM users WHERE last_active_at >= datetime('now', '-7 days')"
            ) as cur:
                active_7d = (await cur.fetchone())["cnt"]

            async with db.execute(
                "SELECT COUNT(*) as cnt FROM users WHERE last_active_at >= datetime('now', '-1 day')"
            ) as cur:
                active_1d = (await cur.fetchone())["cnt"]

            async with db.execute("SELECT COUNT(*) as cnt FROM groups") as cur:
                total_groups = (await cur.fetchone())["cnt"]

            async with db.execute("SELECT COUNT(*) as cnt FROM starostas") as cur:
                total_starostas = (await cur.fetchone())["cnt"]

            async with db.execute("SELECT COUNT(*) as cnt FROM chats") as cur:
                total_chats = (await cur.fetchone())["cnt"]

            async with db.execute("SELECT COUNT(*) as cnt FROM homework") as cur:
                total_hw = (await cur.fetchone())["cnt"]

            async with db.execute("SELECT COUNT(*) as cnt FROM user_notes WHERE reminded = 0") as cur:
                active_notes = (await cur.fetchone())["cnt"]

            async with db.execute("""
                SELECT group_name, COUNT(*) as student_count
                FROM users
                WHERE group_name IS NOT NULL
                GROUP BY group_name
                ORDER BY student_count DESC
                LIMIT 5
            """) as cur:
                top_groups = [dict(r) for r in await cur.fetchall()]

            async with db.execute(
                "SELECT COUNT(*) as cnt FROM users WHERE notifications_enabled = 1"
            ) as cur:
                notif_enabled = (await cur.fetchone())["cnt"]

        return {
            "total_users": total_users,
            "active_7d": active_7d,
            "active_1d": active_1d,
            "total_groups": total_groups,
            "total_starostas": total_starostas,
            "total_chats": total_chats,
            "total_hw": total_hw,
            "active_notes": active_notes,
            "top_groups": top_groups,
            "notif_enabled": notif_enabled,
        }

    async def create_backup_file(self, backup_dir: Optional[str] = None) -> str:
        """
        Создает консистентный снимок базы данных SQLite на диск даже в режиме WAL
        с принудительным сохранением буфера журнала (wal_checkpoint).
        Возвращает путь к созданному файлу бэкапа.
        """
        if not backup_dir:
            backup_dir = os.path.join(os.path.dirname(self.db_path) or ".", "backups")
        os.makedirs(backup_dir, exist_ok=True)

        now_str = datetime.now().strftime("%Y%m%d_%H%M%S")
        backup_path = os.path.abspath(os.path.join(backup_dir, f"ktmu_bot_backup_{now_str}.db"))

        async with aiosqlite.connect(self.db_path) as db:
            try:
                await db.execute("PRAGMA wal_checkpoint(TRUNCATE);")
            except Exception as e:
                logger.warning("Ошибка wal_checkpoint при создании бэкапа: %s", e)

            try:
                escaped_path = backup_path.replace("'", "''")
                await db.execute(f"VACUUM INTO '{escaped_path}';")
            except Exception as e:
                logger.info("VACUUM INTO не поддерживается или завершился с ошибкой (%s), используется shutil.copy2", e)
                import shutil
                shutil.copy2(self.db_path, backup_path)

        return backup_path


# Глобальный синглтон базы данных
db = Database()
