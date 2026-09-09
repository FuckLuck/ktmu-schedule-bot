import asyncio
import json
import logging
import re
from datetime import date, datetime, timedelta
from typing import Any, Optional
from urllib.parse import urljoin

import aiohttp
from bs4 import BeautifulSoup

from config import config
from database import Database, db as default_db

logger = logging.getLogger(__name__)

DAY_NAMES_RU = [
    "Понедельник",
    "Вторник",
    "Среда",
    "Четверг",
    "Пятница",
    "Суббота",
    "Воскресенье",
]

MONTH_NAMES_GENITIVE = [
    "",
    "января",
    "февраля",
    "марта",
    "апреля",
    "мая",
    "июня",
    "июля",
    "августа",
    "сентября",
    "октября",
    "ноября",
    "декабря",
]


class TimetableParser:
    """
    Парсер и менеджер расписания колледжа КТМУ с кэшированием в SQLite.
    Поддерживает:
    - Парсинг HTML-таблиц (BeautifulSoup4)
    - Парсинг структуры API расписания (React SPA timetable-ktmu.ru)
    - Учет семестровых недель, четности, подгрупп и форматов
    """

    def __init__(self, base_url: str = config.BASE_URL, database: Database = default_db):
        self.base_url = base_url.rstrip("/")
        self.db = database
        self.headers = {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                          "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
            "Accept": "application/json,text/html,*/*",
            "Accept-Language": "ru-RU,ru;q=0.9,en-US;q=0.8",
        }
        # Локальный кратковременный кэш конфигурации сайта в памяти
        self._site_data_cache: Optional[dict[str, Any]] = None
        self._site_data_cache_time: Optional[datetime] = None
        # Горячий RAM-кэш готового расписания на день: (group_id, date_str) -> (data, timestamp)
        self._ram_day_cache: dict[tuple[str, str], tuple[dict[str, Any], datetime]] = {}

    def clear_ram_cache(self) -> None:
        """Очищает оперативный кэш расписания в памяти."""
        self._ram_day_cache.clear()
        self._site_data_cache = None
        self._site_data_cache_time = None
        logger.info("Оперативный RAM-кэш расписания успешно очищен.")

    async def _fetch_site_data(self, session: aiohttp.ClientSession) -> dict[str, Any]:
        """
        Загружает полные данные расписания из API хранилища с кэшированием в памяти на 5 минут.
        """
        now = datetime.now()
        if (
            self._site_data_cache
            and self._site_data_cache_time
            and (now - self._site_data_cache_time).total_seconds() < 300
        ):
            return self._site_data_cache

        api_url = f"{self.base_url}/api/storage/schedule-data-v2"
        for attempt in range(3):
            try:
                async with session.get(api_url, headers=self.headers, timeout=aiohttp.ClientTimeout(total=20)) as resp:
                    if resp.status == 503:
                        await asyncio.sleep(1.5 * (attempt + 1))
                        continue
                    resp.raise_for_status()
                    data = await resp.json(content_type=None)
                    val = data.get("value", {})
                    if isinstance(val, str):
                        val = json.loads(val)
                    self._site_data_cache = val
                    self._site_data_cache_time = now
                    return val
            except Exception as e:
                if attempt == 2:
                    if self._site_data_cache:
                        logger.warning("Сайт недоступен (%s), используется устаревший кэш в памяти", e)
                        return self._site_data_cache
                    raise
                await asyncio.sleep(1.5 * (attempt + 1))

        return self._site_data_cache or {}

    def calculate_academic_week(self, target_date: date, semester_start_str: str) -> int:
        """
        Вычисляет номер учебной недели относительно даты начала семестра.
        По умолчанию семестр начинается 1 сентября.
        """
        try:
            sem_start = datetime.strptime(semester_start_str, "%Y-%m-%d").date()
        except Exception:
            sem_start = date(target_date.year if target_date.month >= 8 else target_date.year - 1, 9, 1)

        # Начало первой недели (понедельник недели начала семестра)
        start_monday = sem_start - timedelta(days=sem_start.weekday())
        target_monday = target_date - timedelta(days=target_date.weekday())
        diff_days = (target_monday - start_monday).days
        week_num = (diff_days // 7) + 1
        return max(1, week_num)

    async def _parse_schedule_from_api(
        self, session: aiohttp.ClientSession, group_id: str, target_date: date
    ) -> dict[str, Any]:
        """
        Извлекает расписание группы на целевую дату из модели данных бэкенда.
        """
        site_data = await self._fetch_site_data(session)

        config_data = site_data.get("config", {})
        semester_start = config_data.get("semesterStart", f"{target_date.year}-09-01")
        academic_week = self.calculate_academic_week(target_date, semester_start)

        period_times = config_data.get("periodTimes", config.DEFAULT_PERIOD_TIMES)
        # Очистка неразрывных пробелов и тире
        cleaned_period_times: list[str] = []
        for pt in period_times:
            clean_pt = str(pt).replace("\xa0", " ").replace("–", "-").replace("—", "-").strip()
            cleaned_period_times.append(clean_pt)

        subjects = {s["id"]: s.get("name", "Без названия").strip() for s in site_data.get("subjects", [])}
        teachers = {t["id"]: t.get("name", "").strip() for t in site_data.get("teachers", [])}
        rooms = {r["id"]: r.get("name", "").strip() for r in site_data.get("rooms", [])}

        sched = site_data.get("schedule", {})
        instances = sched.get("instances", [])
        assignments = sched.get("assignment", {})

        target_weekday = target_date.weekday()  # 0 = Monday ... 6 = Sunday

        lessons_map: dict[int, list[dict[str, Any]]] = {}

        for inst in instances:
            inst_group_id = inst.get("groupId")
            stream_group_ids = inst.get("streamGroupIds", [])

            # Проверяем, относится ли пара к нашей группе
            if inst_group_id != group_id and group_id not in stream_group_ids:
                continue

            # Проверка недели (четная/нечетная/кастомные недели)
            week_pattern = inst.get("weekPattern", "every")
            custom_weeks = inst.get("customWeeks", [])

            if custom_weeks and academic_week not in custom_weeks:
                continue
            if week_pattern == "odd" and academic_week % 2 == 0:
                continue
            if week_pattern == "even" and academic_week % 2 == 1:
                continue

            inst_id = inst.get("instId")
            ass = assignments.get(inst_id)
            if not ass:
                continue

            if ass.get("day") != target_weekday:
                continue

            period_idx = int(ass.get("period", 0))
            pair_num = period_idx + 1

            raw_time = cleaned_period_times[period_idx] if period_idx < len(cleaned_period_times) else ""
            if not raw_time and period_idx < len(config.DEFAULT_PERIOD_TIMES):
                raw_time = config.DEFAULT_PERIOD_TIMES[period_idx]

            time_parts = raw_time.split("-")
            start_time = time_parts[0].strip() if len(time_parts) > 0 else ""
            end_time = time_parts[1].strip() if len(time_parts) > 1 else ""

            room_id = ass.get("roomId", "")
            room_name = rooms.get(room_id, "")
            subject_name = subjects.get(inst.get("subjectId"), "Учебное занятие")
            teacher_name = teachers.get(inst.get("teacherId"), "")

            lesson_format = "Дистанционно" if inst.get("format") == "remote" else "Очно"
            subgroup = inst.get("subgroup", 0)

            lesson_item = {
                "pair_number": pair_num,
                "time": raw_time,
                "start_time": start_time,
                "end_time": end_time,
                "subject": subject_name,
                "teacher": teacher_name,
                "room": room_name,
                "format": lesson_format,
                "subgroup": subgroup,
            }

            lessons_map.setdefault(pair_num, []).append(lesson_item)

        # Сортируем пары по номеру
        sorted_lessons: list[dict[str, Any]] = []
        for p_num in sorted(lessons_map.keys()):
            sorted_lessons.extend(lessons_map[p_num])

        return {
            "date": target_date.isoformat(),
            "day_name": DAY_NAMES_RU[target_weekday],
            "week_number": academic_week,
            "is_even_week": academic_week % 2 == 0,
            "lessons": sorted_lessons,
        }

    async def _parse_schedule_from_html(
        self, session: aiohttp.ClientSession, group_url: str, target_date: date
    ) -> Optional[dict[str, Any]]:
        """
        Пытается спарсить расписание со страницы группы по group_url через BeautifulSoup4,
        если страница отдается в виде статического HTML.
        """
        full_url = urljoin(self.base_url, group_url)
        try:
            async with session.get(full_url, headers=self.headers, timeout=aiohttp.ClientTimeout(total=10)) as resp:
                if resp.status != 200:
                    return None
                html = await resp.text()

            soup = BeautifulSoup(html, "html.parser")
            # Проверяем, есть ли таблица или карточки расписания
            timetable_blocks = soup.find_all(class_=re.compile(r"schedule|timetable|lesson|pair", re.I))
            if not timetable_blocks:
                return None

            lessons: list[dict[str, Any]] = []
            for block in timetable_blocks:
                text = block.get_text(" ", strip=True)
                if not text:
                    continue
                # Извлекаем время и название
                time_match = re.search(r"(\d{1,2}:\d{2})\s*[-—–]\s*(\d{1,2}:\d{2})", text)
                time_str = time_match.group(0) if time_match else ""
                lessons.append({
                    "pair_number": len(lessons) + 1,
                    "time": time_str,
                    "start_time": time_match.group(1) if time_match else "",
                    "end_time": time_match.group(2) if time_match else "",
                    "subject": text[:80],
                    "teacher": "",
                    "room": "",
                    "format": "Очно",
                    "subgroup": 0,
                })

            if lessons:
                target_weekday = target_date.weekday()
                return {
                    "date": target_date.isoformat(),
                    "day_name": DAY_NAMES_RU[target_weekday],
                    "week_number": 1,
                    "is_even_week": False,
                    "lessons": lessons,
                }
        except Exception as e:
            logger.debug("HTML разбор расписания по ссылке %s не удался: %s", full_url, e)

        return None

    async def fetch_day_schedule(
        self,
        group_id: str,
        group_url: str = "",
        target_date: Optional[date] = None,
        force_refresh: bool = False,
    ) -> dict[str, Any]:
        """
        Получает расписание для указанной группы на целевой день с мгновенной отдачей:
        1. Проверяет горячий RAM-кэш в памяти (0 мс).
        2. Проверяет SQLite кэш (1 мс).
        3. Если кэша нет, запрашивает API модели сайта напрямую (без медленных запросов к HTML).
        4. Сохраняет результат в SQLite и RAM кэш.
        """
        if target_date is None:
            target_date = date.today()

        date_str = target_date.isoformat()
        cache_key = (group_id, date_str)

        # 1. Проверка оперативного RAM-кэша (0 мс)
        if not force_refresh and cache_key in self._ram_day_cache:
            cached_data, cached_time = self._ram_day_cache[cache_key]
            # Время жизни RAM-кэша 1 час
            if (datetime.now() - cached_time).total_seconds() < 3600:
                logger.debug("Мгновенная отдача из RAM кэша: группа %s на %s", group_id, date_str)
                return cached_data

        # 2. Проверка SQLite кэша
        if not force_refresh:
            cached = await self.db.get_cached_timetable(group_id, date_str)
            if cached:
                self._ram_day_cache[cache_key] = (cached, datetime.now())
                logger.debug("Расписание получено из SQLite кэша: группа %s на %s", group_id, date_str)
                return cached

        # 3. Получение данных с сайта: API-First (быстрый JSON без ожидания пустого HTML)
        logger.info("Запрос расписания с сайта для группы %s на %s ...", group_id, date_str)
        schedule_data: Optional[dict[str, Any]] = None

        async with aiohttp.ClientSession() as session:
            try:
                # Первым делом запрашиваем API модели (быстро, структурированно, без задержек)
                schedule_data = await self._parse_schedule_from_api(session, group_id, target_date)
            except Exception as e:
                logger.warning("Ошибка получения расписания из API для группы %s: %s", group_id, e)

            # Если API вернул пустые пары или упал, и есть group_url, пробуем HTML как fallback
            if (not schedule_data or not schedule_data.get("lessons")) and group_url:
                try:
                    html_data = await self._parse_schedule_from_html(session, group_url, target_date)
                    if html_data and html_data.get("lessons"):
                        schedule_data = html_data
                except Exception as e:
                    logger.debug("HTML fallback не удался: %s", e)

        if not schedule_data:
            schedule_data = {
                "date": date_str,
                "day_name": DAY_NAMES_RU[target_date.weekday()],
                "week_number": 1,
                "is_even_week": False,
                "lessons": [],
            }

        # 4. Сохранение в SQLite кэш и оперативный RAM-кэш
        await self.db.save_cached_timetable(group_id, date_str, schedule_data)
        self._ram_day_cache[cache_key] = (schedule_data, datetime.now())

        return schedule_data

    async def fetch_week_schedule(
        self,
        group_id: str,
        group_url: str = "",
        start_date: Optional[date] = None,
        force_refresh: bool = False,
    ) -> list[dict[str, Any]]:
        """
        Получает расписание на всю учебную неделю (с понедельника по субботу).
        Заранее прогревает и кэширует каждый день.
        """
        if start_date is None:
            start_date = date.today()

        monday = start_date - timedelta(days=start_date.weekday())
        week_schedule: list[dict[str, Any]] = []

        # Обходим 6 учебных дней: понедельник ... суббота
        for i in range(6):
            day_date = monday + timedelta(days=i)
            day_data = await self.fetch_day_schedule(
                group_id=group_id,
                group_url=group_url,
                target_date=day_date,
                force_refresh=force_refresh,
            )
            week_schedule.append(day_data)

        return week_schedule

    async def preload_group_schedule(self, group_id: str, group_url: str = "", days_ahead: int = 7) -> None:
        """
        Фоновый прогрев расписания группы на ближайшие дни вперед (сегодня, завтра и неделя).
        Позволяет отдавать расписание мгновенно без малейшей паузы для студента.
        """
        try:
            today = date.today()
            logger.info("Фоновый прогрев кэша для группы %s на %d дней...", group_id, days_ahead)
            for offset in range(days_ahead):
                target_date = today + timedelta(days=offset)
                await self.fetch_day_schedule(
                    group_id=group_id,
                    group_url=group_url,
                    target_date=target_date,
                    force_refresh=False,
                )
            logger.info("Кэш группы %s успешно прогрет!", group_id)
        except Exception as e:
            logger.warning("Не удалось прогреть кэш для группы %s: %s", group_id, e)

    async def preload_active_groups_schedules(self, database: Database) -> None:
        """
        Фоновый прогрев расписания для всех групп, на которые подписаны реальные пользователи.
        """
        try:
            active_groups = await database.get_active_group_ids()
            if not active_groups:
                return
            logger.info("Запуск фонового прогрева расписания для %d активных групп...", len(active_groups))
            for group_id in active_groups:
                await self.preload_group_schedule(group_id, days_ahead=4)
                await asyncio.sleep(0.1)  # микропауза между группами
            logger.info("Фоновый прогрев всех активных групп завершен!")
        except Exception as e:
            logger.warning("Ошибка фонового прогрева активных групп: %s", e)


# -------------------------------------------------------------------------
# ФУНКЦИИ ФОРМАТИРОВАНИЯ СООБЩЕНИЙ ДЛЯ TELEGRAM
# -------------------------------------------------------------------------

def format_day_schedule_message(schedule_data: dict[str, Any], group_name: str) -> str:
    """
    Форматирует расписание на день в стильное читабельное сообщение с эмодзи.
    """
    date_str = schedule_data.get("date", "")
    day_name = schedule_data.get("day_name", "")
    week_num = schedule_data.get("week_number", "")
    parity_str = "четная" if schedule_data.get("is_even_week") else "нечетная"
    lessons = schedule_data.get("lessons", [])

    formatted_date = date_str
    try:
        dt = datetime.strptime(date_str, "%Y-%m-%d")
        formatted_date = f"{dt.day} {MONTH_NAMES_GENITIVE[dt.month]} {dt.year}"
    except Exception:
        pass

    lines = [
        f"📅 <b>Расписание на {day_name}</b>, {formatted_date}",
        f"👥 <b>Группа:</b> <code>{group_name}</code>",
        f"🔢 <b>Неделя:</b> {week_num} ({parity_str})",
        "────────────────────",
    ]

    if not lessons:
        lines.append("\n🎉 <b>В этот день пар нет! Отдыхайте.</b>")
        return "\n".join(lines)

    for idx, l in enumerate(lessons, start=1):
        pair_num = l.get("pair_number", idx)
        time_str = l.get("time", "")
        subject = l.get("subject", "Без названия")
        room = l.get("room", "")
        teacher = l.get("teacher", "")
        fmt = l.get("format", "Очно")
        subgroup = l.get("subgroup", 0)

        subgroup_str = f" [Подгруппа {subgroup}]" if subgroup else ""
        format_icon = "🌐" if fmt == "Дистанционно" else "🏛"

        lines.append(f"\n🔹 <b>{pair_num} пара</b> ({time_str})")
        lines.append(f"📖 <b>{subject}</b>{subgroup_str}")

        details = []
        if room:
            details.append(f"🚪 <b>Ауд:</b> {room}")
        if teacher:
            details.append(f"👤 <b>Преп:</b> {teacher}")
        details.append(f"{format_icon} <i>{fmt}</i>")

        lines.append(" | ".join(details))

    return "\n".join(lines)


def format_week_schedule_messages(week_data: list[dict[str, Any]], group_name: str) -> list[str]:
    """
    Форматирует расписание на неделю, разбивая на порции сообщений
    для соблюдения лимита Telegram (4096 символов).
    """
    messages: list[str] = []
    first_date = week_data[0].get("date", "") if week_data else ""
    week_num = week_data[0].get("week_number", "") if week_data else ""
    parity_str = "четная" if (week_data and week_data[0].get("is_even_week")) else "нечетная"

    header = (
        f"🗓 <b>Расписание на неделю для группы</b> <code>{group_name}</code>\n"
        f"🔢 <b>Учебная неделя:</b> {week_num} ({parity_str})\n"
        f"════════════════════\n"
    )

    current_chunk = header
    for day_data in week_data:
        day_text = format_day_schedule_message(day_data, group_name)
        # Отсекаем повторный заголовок группы из дня
        day_lines = day_text.split("\n")
        filtered_lines = [line for line in day_lines if not line.startswith("👥 <b>Группа:</b>") and not line.startswith("🔢 <b>Неделя:</b>")]
        day_block = "\n" + "\n".join(filtered_lines) + "\n"

        if len(current_chunk) + len(day_block) > 3800:
            messages.append(current_chunk)
            current_chunk = day_block
        else:
            current_chunk += day_block

    if current_chunk.strip():
        messages.append(current_chunk)

    return messages


def format_pair_notification(lesson: dict[str, Any], group_name: str) -> str:
    """
    Форматирует срочное уведомление о начале пары за 0 минут до звонка.
    """
    pair_num = lesson.get("pair_number", "")
    time_str = lesson.get("time", "")
    subject = lesson.get("subject", "Занятие")
    room = lesson.get("room", "Не указана")
    teacher = lesson.get("teacher", "Не указан")
    fmt = lesson.get("format", "Очно")
    subgroup = lesson.get("subgroup", 0)

    subgroup_str = f" [Подгруппа {subgroup}]" if subgroup else ""
    format_icon = "🌐" if fmt == "Дистанционно" else "🏛"

    return (
        f"🔔 <b>Внимание! Пара началась</b>\n"
        f"👥 <b>Группа:</b> <code>{group_name}</code>\n\n"
        f"🔹 <b>{pair_num} пара</b> ({time_str})\n"
        f"📖 <b>{subject}</b>{subgroup_str}\n"
        f"🚪 <b>Аудитория:</b> {room}\n"
        f"👤 <b>Преподаватель:</b> {teacher}\n"
        f"{format_icon} <b>Формат:</b> {fmt}"
    )


# Глобальный экземпляр парсера расписания
timetable_parser = TimetableParser()
