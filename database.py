import aiosqlite
import json
import logging
from datetime import datetime, timezone
from typing import Any, Optional

from config import config

logger = logging.getLogger(__name__)


class Database:
    """
    Асинхронный слой работы с базой данных SQLite через aiosqlite.
    Поддерживает управление пользователями, группами и кэширование расписания.
    """

    def __init__(self, db_path: str = config.DATABASE_PATH):
        self.db_path = db_path

    async def init_db(self) -> None:
        """
        Инициализирует таблицы базы данных в строгом соответствии с ТЗ.
        """
        async with aiosqlite.connect(self.db_path) as db:
            await db.execute("PRAGMA journal_mode=WAL;")
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
                await db.execute("ALTER TABLE users ADD COLUMN created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP;")
            if "last_active_at" not in columns:
                await db.execute("ALTER TABLE users ADD COLUMN last_active_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP;")

            # 2. Таблица groups
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

            # 3. Таблица timetable_cache
            await db.execute("""
                CREATE TABLE IF NOT EXISTS timetable_cache (
                    group_id TEXT NOT NULL,
                    date TEXT NOT NULL,
                    data_json TEXT NOT NULL,
                    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    PRIMARY KEY (group_id, date)
                );
            """)

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
                "SELECT user_id, group_id, group_url, group_name, notifications_enabled, username, first_name, created_at, last_active_at FROM users WHERE user_id = ?",
                (user_id,)
            ) as cursor:
                row = await cursor.fetchone()
                if row:
                    return {
                        "user_id": row["user_id"],
                        "group_id": row["group_id"],
                        "group_url": row["group_url"],
                        "group_name": row["group_name"],
                        "notifications_enabled": bool(row["notifications_enabled"]),
                        "username": row["username"],
                        "first_name": row["first_name"],
                        "created_at": row["created_at"],
                        "last_active_at": row["last_active_at"],
                    }
                return None

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
        Возвращает общую статистику пользователей для админ-панели.
        """
        async with aiosqlite.connect(self.db_path) as db:
            async with db.execute("SELECT COUNT(*) FROM users;") as cur:
                total_users = (await cur.fetchone())[0]
            async with db.execute("SELECT COUNT(*) FROM users WHERE group_id IS NOT NULL AND group_id != '';") as cur:
                with_group = (await cur.fetchone())[0]
            async with db.execute("SELECT COUNT(*) FROM users WHERE notifications_enabled = 1;") as cur:
                notifications_on = (await cur.fetchone())[0]
            async with db.execute("SELECT COUNT(*) FROM groups;") as cur:
                total_groups = (await cur.fetchone())[0]
            return {
                "total_users": total_users,
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
        Возвращает список уникальных group_id выбранных студентами групп.
        """
        async with aiosqlite.connect(self.db_path) as db:
            async with db.execute(
                "SELECT DISTINCT group_id FROM users WHERE group_id IS NOT NULL AND group_id != '';"
            ) as cur:
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

    async def save_cached_timetable(self, group_id: str, date_str: str, data: dict[str, Any]) -> None:
        """
        Сохраняет расписание в кэш timetable_cache.
        """
        data_json = json.dumps(data, ensure_ascii=False)
        now_iso = datetime.now(timezone.utc).isoformat()
        async with aiosqlite.connect(self.db_path) as db:
            await db.execute("""
                INSERT INTO timetable_cache (group_id, date, data_json, updated_at)
                VALUES (?, ?, ?, ?)
                ON CONFLICT(group_id, date) DO UPDATE SET
                    data_json = excluded.data_json,
                    updated_at = excluded.updated_at;
            """, (group_id, date_str, data_json, now_iso))
            await db.commit()
            logger.debug("Расписание сохранено в кэш для %s на %s", group_id, date_str)

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


# Глобальный синглтон базы данных
db = Database()
