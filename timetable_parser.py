import asyncio
import hashlib
import json
import logging
import re
from datetime import date, datetime, time, timedelta
from typing import Any, Optional
from urllib.parse import urljoin
from zoneinfo import ZoneInfo

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


def clean_period_time(pt: str) -> str:
    """
    Очищает строку времени от неразрывных пробелов и тире, нормализует в формат HH:MM-HH:MM.
    Например: '8:30–9:55' -> '08:30-09:55', '15:20–16:45' -> '15:20-16:45'.
    """
    if not pt:
        return ""
    cleaned = str(pt).replace("\xa0", " ").replace("–", "-").replace("—", "-").strip()
    if "-" in cleaned:
        parts = cleaned.split("-", 1)
        start_raw = parts[0].strip()
        end_raw = parts[1].strip()

        def _fmt(s: str) -> str:
            sp = s.split(":")
            if len(sp) == 2 and sp[0].isdigit() and sp[1].isdigit():
                return f"{int(sp[0]):02d}:{int(sp[1]):02d}"
            return s

        return f"{_fmt(start_raw)}-{_fmt(end_raw)}"
    return cleaned


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
        # Долгоживущая переиспользуемая HTTP-сессия с пулом соединений
        self._session: Optional[aiohttp.ClientSession] = None

    async def get_session(self) -> aiohttp.ClientSession:
        """
        Возвращает долгоживущую HTTP-сессию aiohttp с пулом соединений (connection pooling) и Keep-Alive.
        Устраняет накладные расходы на постоянное создание сокетов, DNS-резолвинг и TLS handshakes.
        """
        if self._session is None or self._session.closed:
            connector = aiohttp.TCPConnector(
                limit=100,
                limit_per_host=20,
                keepalive_timeout=60,
            )
            self._session = aiohttp.ClientSession(
                connector=connector,
                headers=self.headers,
                timeout=aiohttp.ClientTimeout(total=20, connect=5),
            )
        return self._session

    async def close(self) -> None:
        """Корректно закрывает активную сессию и пул соединений при остановке бота."""
        if self._session and not self._session.closed:
            await self._session.close()
            self._session = None
            logger.info("Сетевой пул соединений TimetableParser успешно закрыт.")

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

        # Стандартные звонки колледжа
        period_times = config_data.get("periodTimes", config.DEFAULT_PERIOD_TIMES)
        cleaned_period_times = [clean_period_time(pt) for pt in period_times]

        # Звонки для корпуса на Вознесенском пр., 44
        voz_period_times = config_data.get("voznesenskyPeriodTimes", config.DEFAULT_VOZNESENSKY_PERIOD_TIMES)
        cleaned_voz_period_times = [clean_period_time(pt) for pt in voz_period_times]
        if not any(cleaned_voz_period_times):
            cleaned_voz_period_times = [clean_period_time(pt) for pt in config.DEFAULT_VOZNESENSKY_PERIOD_TIMES]

        # Сокращенные звонки
        shortened_period_times = [clean_period_time(pt) for pt in config_data.get("shortenedPeriodTimes", [])]
        shortened_dates = set(config_data.get("shortenedDates", []))

        # Определение четности недели с учетом возможных исключений колледжа
        target_date_str = target_date.isoformat()
        week_parity_overrides = config_data.get("weekParityOverrides", {})
        if target_date_str in week_parity_overrides:
            week_parity = week_parity_overrides[target_date_str]
        else:
            week_parity = "even" if academic_week % 2 == 0 else "odd"

        # Получаем данные текущей группы из API для проверки кастомных звонков
        groups_list = site_data.get("groups", [])
        current_group_data = next((g for g in groups_list if g.get("id") == group_id), {})
        group_day_period_times_by_parity = current_group_data.get("dayPeriodTimesByParity") or {}
        group_day_period_times = current_group_data.get("dayPeriodTimes") or {}

        subjects = {s["id"]: s.get("name", "Без названия").strip() for s in site_data.get("subjects", [])}
        teachers = {t["id"]: t.get("name", "").strip() for t in site_data.get("teachers", [])}
        rooms_dict = {r["id"]: r for r in site_data.get("rooms", [])}
        lesson_types = {t["id"]: t.get("name", "").strip() for t in site_data.get("lessonTypes", [])}

        type_mapping = {
            "лек": "Лекция",
            "лекция": "Лекция",
            "пр": "Практика",
            "практика": "Практика",
            "лаб": "Лабораторная",
            "лабораторная": "Лабораторная",
            "контрработа": "Контрольная работа",
            "контрольная работа": "Контрольная работа",
            "зч": "Зачёт",
            "зачёт": "Зачёт",
            "зачет": "Зачёт",
            "зчо": "Дифф. зачёт",
            "дифференцированный зачёт": "Дифф. зачёт",
            "курсработа": "Курсовая работа",
            "индпроект": "Инд. проект",
        }

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

            room_id = ass.get("roomId", "")
            room_obj = rooms_dict.get(room_id, {})
            room_name = room_obj.get("name", "").strip() if room_id else ""
            is_external_room = bool(
                room_obj.get("isExternal")
                or (room_name and (room_name.startswith(("В-", "В ", "в-", "в ")) or "вознесен" in room_name.lower()))
            )

            # --- Точное вычисление времени пары (звонков) ---
            raw_time = ""

            # 1. Специфичные звонки группы для текущей четности недели (odd/even)
            parity_dict = group_day_period_times_by_parity.get(week_parity) or {}
            parity_times = parity_dict.get(str(target_weekday)) or parity_dict.get(target_weekday)
            if parity_times and period_idx < len(parity_times) and parity_times[period_idx]:
                raw_time = clean_period_time(parity_times[period_idx])

            # 2. Общие звонки группы на этот день недели
            if not raw_time:
                day_times = group_day_period_times.get(str(target_weekday)) or group_day_period_times.get(target_weekday)
                if day_times and period_idx < len(day_times) and day_times[period_idx]:
                    raw_time = clean_period_time(day_times[period_idx])

            # 3. Если пара проходит в корпусе на Вознесенском пр., 44
            if not raw_time and is_external_room:
                if period_idx < len(cleaned_voz_period_times) and cleaned_voz_period_times[period_idx]:
                    raw_time = cleaned_voz_period_times[period_idx]

            # 4. Сокращенный день
            if not raw_time and target_date_str in shortened_dates:
                if period_idx < len(shortened_period_times) and shortened_period_times[period_idx]:
                    raw_time = shortened_period_times[period_idx]

            # 5. Базовые звонки колледжа
            if not raw_time:
                if period_idx < len(cleaned_period_times) and cleaned_period_times[period_idx]:
                    raw_time = cleaned_period_times[period_idx]
                elif period_idx < len(config.DEFAULT_PERIOD_TIMES):
                    raw_time = clean_period_time(config.DEFAULT_PERIOD_TIMES[period_idx])

            time_parts = raw_time.split("-")
            start_time = time_parts[0].strip() if len(time_parts) > 0 else ""
            end_time = time_parts[1].strip() if len(time_parts) > 1 else ""

            subject_name = subjects.get(inst.get("subjectId"), "Учебное занятие")
            teacher_name = teachers.get(inst.get("teacherId"), "")

            type_id = inst.get("typeId", "")
            raw_type_name = lesson_types.get(type_id, "").strip()
            lesson_type = type_mapping.get(raw_type_name.lower(), raw_type_name or "Занятие")

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
                "lesson_type": lesson_type,
                "is_external": is_external_room,
                "location": "Вознесенский пр., 44" if is_external_room else "Основной корпус",
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

        session = await self.get_session()
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
        schedule_hash = compute_schedule_hash(schedule_data)
        await self.db.save_cached_timetable(group_id, date_str, schedule_data, data_hash=schedule_hash)
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
# ФУНКЦИИ ФОРМАТИРОВАНИЯ СООБЩЕНИЙ И ХЭШИРОВАНИЯ
# -------------------------------------------------------------------------

def compute_schedule_hash(schedule_data: dict[str, Any] | list[dict[str, Any]]) -> str:
    """
    Вычисляет стабильный MD5-хэш структуры пар дня для мониторинга замен и изменений.
    """
    if isinstance(schedule_data, list):
        lessons = schedule_data
    elif isinstance(schedule_data, dict):
        lessons = schedule_data.get("lessons", [])
    else:
        lessons = []
    canonical = json.dumps(lessons, sort_keys=True, ensure_ascii=False)
    return hashlib.md5(canonical.encode("utf-8")).hexdigest()


def format_day_schedule_message(schedule_data: dict[str, Any], group_name: str) -> str:
    """
    Форматирует расписание на день в читабельное сообщение с эмодзи.
    Группирует подгруппы по номеру пары в древовидную структуру без дублирования заголовка.
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

    # Группируем пары по номеру пары и времени
    pairs_map: dict[tuple[int, str], list[dict[str, Any]]] = {}
    for idx, l in enumerate(lessons, start=1):
        p_num = l.get("pair_number", idx)
        p_time = l.get("time", "")
        pairs_map.setdefault((p_num, p_time), []).append(l)

    for (pair_num, time_str), pair_lessons in pairs_map.items():
        time_part = f" ({time_str})" if time_str else ""
        first_l_type = pair_lessons[0].get("lesson_type", "")
        type_badge = f" • <b>[{first_l_type}]</b>" if first_l_type and first_l_type != "Занятие" else ""
        pair_header = f"\n🔹 <b>{pair_num} пара</b>{time_part}{type_badge}"

        if len(pair_lessons) > 1:
            # Несколько записей для одного времени пары (деление на подгруппы)
            pair_lessons.sort(key=lambda x: x.get("subgroup", 0))
            subjects = [l.get("subject", "Учебное занятие") for l in pair_lessons]
            # Проверяем, совпадает ли предмет у подгрупп
            first_subj = subjects[0]
            same_subject = all(s.strip().lower() == first_subj.strip().lower() for s in subjects)

            lines.append(pair_header)
            if same_subject:
                lines.append(f"📖 <b>{first_subj}</b>")
                for i, l in enumerate(pair_lessons):
                    is_last = (i == len(pair_lessons) - 1)
                    branch = "└── " if is_last else "├── "
                    sub = l.get("subgroup", i + 1)
                    room = l.get("room", "")
                    teacher = l.get("teacher", "")
                    sub_type = l.get("lesson_type", "")

                    parts = []
                    if sub_type and sub_type != "Занятие":
                        parts.append(f"[{sub_type}]")
                    if room:
                        room_label = f"Ауд. {room}"
                        if l.get("is_external") and "Вознесенск" not in room:
                            room_label += " (Вознесенский)"
                        parts.append(room_label)
                    if teacher:
                        parts.append(f"👤 {teacher}")
                    details_str = " | ".join(parts) if parts else "По расписанию"

                    lines.append(f"{branch}👥 {sub} подгруппа: {details_str}")
            else:
                for i, l in enumerate(pair_lessons):
                    is_last = (i == len(pair_lessons) - 1)
                    branch = "└── " if is_last else "├── "
                    sub = l.get("subgroup", i + 1)
                    subj = l.get("subject", "Занятие")
                    room = l.get("room", "")
                    teacher = l.get("teacher", "")
                    sub_type = l.get("lesson_type", "")

                    parts = []
                    if sub_type and sub_type != "Занятие":
                        parts.append(f"[{sub_type}]")
                    if room:
                        room_label = f"Ауд. {room}"
                        if l.get("is_external") and "Вознесенск" not in room:
                            room_label += " (Вознесенский)"
                        parts.append(room_label)
                    if teacher:
                        parts.append(f"👤 {teacher}")
                    details_str = " | ".join(parts) if parts else "По расписанию"

                    lines.append(f"{branch}👥 {sub} подгруппа ({subj}): {details_str}")
        else:
            # Одиночный вывод для всей группы целиком
            l = pair_lessons[0]
            subject = l.get("subject", "Без названия")
            room = l.get("room", "")
            teacher = l.get("teacher", "")
            fmt = l.get("format", "Очно")
            subgroup = l.get("subgroup", 0)
            l_type = l.get("lesson_type", "")

            subgroup_str = f" [Подгруппа {subgroup}]" if subgroup else ""
            format_icon = "🌐" if fmt == "Дистанционно" else "🏛"

            lines.append(pair_header)
            lines.append(f"📖 <b>{subject}</b>{subgroup_str}")

            details = []
            if l_type and l_type != "Занятие":
                details.append(f"🏷️ <i>{l_type}</i>")
            if room:
                room_text = f"🚪 <b>Ауд:</b> {room}"
                if l.get("is_external") and "Вознесенск" not in room:
                    room_text += " <i>(Вознесенский пр., 44)</i>"
                details.append(room_text)
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
    Требование: «🔔 Началась N пара! Предмет: X, Кабинет: Y».
    """
    pair_num = lesson.get("pair_number", "")
    time_str = lesson.get("time", "")
    subject = lesson.get("subject", "Занятие")
    room = lesson.get("room", "Не указан")
    teacher = lesson.get("teacher", "")
    fmt = lesson.get("format", "Очно")
    subgroup = lesson.get("subgroup", 0)

    time_info = f" ({time_str})" if time_str else ""
    format_icon = "🌐" if fmt == "Дистанционно" else "🏛"
    room_display = f"{room} (Вознесенский пр., 44)" if (lesson.get("is_external") and room and "Вознесенск" not in room) else room

    lines = [
        f"🔔 <b>Началась {pair_num} пара! Предмет: {subject}, Кабинет: {room_display}</b>",
        f"👥 <b>Группа:</b> <code>{group_name}</code>{time_info}",
    ]
    details = []
    if teacher:
        details.append(f"👤 <b>Преподаватель:</b> {teacher}")
    if subgroup:
        details.append(f"👥 <b>Подгруппа:</b> {subgroup}")
    details.append(f"{format_icon} <i>{fmt}</i>")
    if details:
        lines.append(" | ".join(details))

    return "\n".join(lines)


def compute_schedule_diff(
    old_sched: Optional[dict[str, Any]], new_sched: dict[str, Any]
) -> list[str]:
    """
    Сравнивает старый и новый список пар на день и возвращает форматированный список изменений.
    Определяет:
    - Отмену пары
    - Добавление пары
    - Замену предмета
    - Смену аудитории
    - Смену преподавателя
    - Изменение времени
    """
    if not old_sched or not isinstance(old_sched, dict):
        return []

    old_lessons = old_sched.get("lessons", [])
    new_lessons = new_sched.get("lessons", [])

    if not old_lessons and not new_lessons:
        return []

    def _make_key(l: dict[str, Any]) -> tuple[int, int]:
        return (int(l.get("pair_number") or 0), int(l.get("subgroup") or 0))

    old_map: dict[tuple[int, int], dict[str, Any]] = {}
    for l in old_lessons:
        old_map[_make_key(l)] = l

    new_map: dict[tuple[int, int], dict[str, Any]] = {}
    for l in new_lessons:
        new_map[_make_key(l)] = l

    all_keys = sorted(set(old_map.keys()) | set(new_map.keys()))
    diffs: list[str] = []

    for key in all_keys:
        pair_num, sub = key
        sub_str = f" [{sub} подгр.]" if sub else ""

        if key in old_map and key not in new_map:
            old_l = old_map[key]
            subj = old_l.get("subject", "Занятие")
            room = old_l.get("room", "")
            room_str = f" (каб. {room})" if room and room != "—" else ""
            diffs.append(f"❌ <b>Пара №{pair_num}{sub_str}</b>: <s>{subj}</s>{room_str} — <b>ОТМЕНЕНА</b>")

        elif key not in old_map and key in new_map:
            new_l = new_map[key]
            subj = new_l.get("subject", "Занятие")
            room = new_l.get("room", "")
            t_str = new_l.get("time", "")
            tch = new_l.get("teacher", "")
            details = []
            if t_str:
                details.append(t_str)
            if room and room != "—":
                details.append(f"каб. {room}")
            if tch and tch != "—":
                details.append(tch)
            info = f" ({', '.join(details)})" if details else ""
            diffs.append(f"➕ <b>Пара №{pair_num}{sub_str}</b>: <b>{subj}</b>{info} — <b>ДОБАВЛЕНА</b>")

        else:
            old_l = old_map[key]
            new_l = new_map[key]

            old_subj = (old_l.get("subject") or "").strip()
            new_subj = (new_l.get("subject") or "").strip()
            old_room = (old_l.get("room") or "").strip()
            new_room = (new_l.get("room") or "").strip()
            old_tch = (old_l.get("teacher") or "").strip()
            new_tch = (new_l.get("teacher") or "").strip()
            old_time = (old_l.get("time") or "").strip()
            new_time = (new_l.get("time") or "").strip()

            if old_subj != new_subj:
                old_info = f"каб. {old_room}" if old_room and old_room != "—" else ""
                new_info = f"каб. {new_room}" if new_room and new_room != "—" else ""
                diffs.append(
                    f"🔄 <b>Пара №{pair_num}{sub_str}</b>: <b>Замена предмета!</b>\n"
                    f"   Было: <s>{old_subj}</s> ({old_info})\n"
                    f"   Стало: <b>{new_subj}</b> ({new_info})"
                )
            else:
                pair_changes: list[str] = []
                if old_room != new_room and new_room:
                    pair_changes.append(f"🚪 каб. <s>{old_room or '—'}</s> → <b>{new_room}</b>")
                if old_tch != new_tch and new_tch:
                    pair_changes.append(f"👤 преп. <s>{old_tch or '—'}</s> → <b>{new_tch}</b>")
                if old_time != new_time and new_time:
                    pair_changes.append(f"⏰ время <s>{old_time}</s> → <b>{new_time}</b>")

                if pair_changes:
                    diffs.append(
                        f"📝 <b>Пара №{pair_num}{sub_str} ({new_subj})</b>: " + "; ".join(pair_changes)
                    )

    return diffs


def get_current_pair_status_info(
    sched: dict[str, Any], current_dt: datetime, group_name: str, lang: str = "ru"
) -> str:
    """
    Рассчитывает в реальном времени текущее состояние учебного процесса:
    - До начала первой пары (обратный отсчет)
    - Во время пары (сколько минут осталось до звонка, следующая пара)
    - На перемене (сколько минут осталось отдыхать, куда идти)
    - После окончания всех пар
    - Выходной день
    """
    lessons = sched.get("lessons", [])
    if not lessons:
        if lang == "en":
            return f"🎉 <b>No classes scheduled for today!</b>\nEnjoy your rest, group <code>{group_name}</code>!"
        return f"🎉 <b>Сегодня занятий нет!</b>\nОтдыхайте и набирайтесь сил, группа <code>{group_name}</code>!"

    def _parse_time_str(t_str: str) -> Optional[time]:
        try:
            parts = t_str.strip().split(":")
            return time(int(parts[0]), int(parts[1]))
        except Exception:
            return None

    pairs_dict: dict[int, list[dict[str, Any]]] = {}
    for l in lessons:
        p_num = int(l.get("pair_number") or 1)
        pairs_dict.setdefault(p_num, []).append(l)

    sorted_pair_nums = sorted(pairs_dict.keys())
    pair_blocks: list[dict[str, Any]] = []

    for p_num in sorted_pair_nums:
        group_lessons = pairs_dict[p_num]
        first_l = group_lessons[0]
        start_t = _parse_time_str(first_l.get("start_time", ""))
        end_t = _parse_time_str(first_l.get("end_time", ""))

        if not start_t or not end_t:
            raw_time = first_l.get("time", "")
            if "-" in raw_time:
                t_parts = raw_time.split("-")
                start_t = _parse_time_str(t_parts[0])
                end_t = _parse_time_str(t_parts[1])

        if not start_t or not end_t:
            continue

        start_dt = datetime.combine(current_dt.date(), start_t, tzinfo=current_dt.tzinfo)
        end_dt = datetime.combine(current_dt.date(), end_t, tzinfo=current_dt.tzinfo)

        pair_blocks.append({
            "pair_number": p_num,
            "start_dt": start_dt,
            "end_dt": end_dt,
            "start_str": start_t.strftime("%H:%M"),
            "end_str": end_t.strftime("%H:%M"),
            "lessons": group_lessons,
        })

    if not pair_blocks:
        return format_day_schedule_message(sched, group_name)

    def _format_pair_details(block: dict[str, Any]) -> str:
        lines = []
        for l in block["lessons"]:
            subj = l.get("subject", "Занятие")
            room = l.get("room", "Не указан")
            loc = l.get("location", "")
            tch = l.get("teacher", "")
            sub = l.get("subgroup", 0)
            is_ext = l.get("is_external", False)

            room_disp = f"каб. {room}" if room and room != "—" else "кабинет не указан"
            if is_ext and "Вознесенск" not in loc:
                loc_disp = " (Вознесенский пр., 44)"
            elif loc:
                loc_disp = f" ({loc})"
            else:
                loc_disp = ""

            sub_disp = f" [подгр. {sub}]" if sub else ""
            tch_disp = f"\n   👤 <i>{tch}</i>" if tch and tch != "—" else ""

            lines.append(f"• <b>{subj}</b>{sub_disp}\n   🚪 <b>{room_disp}</b>{loc_disp}{tch_disp}")
        return "\n".join(lines)

    first_block = pair_blocks[0]
    last_block = pair_blocks[-1]

    # 1. До начала всех пар
    if current_dt < first_block["start_dt"]:
        mins_until = max(1, int((first_block["start_dt"] - current_dt).total_seconds() // 60))
        h = mins_until // 60
        m = mins_until % 60
        time_until_str = f"{h} ч. {m} мин." if h > 0 else f"{m} мин."

        return (
            f"🌅 <b>Пары еще не начались!</b>\n"
            f"👥 Группа: <code>{group_name}</code>\n"
            f"⏳ До начала 1-й пары осталось: <b>{time_until_str}</b> (начало в {first_block['start_str']})\n\n"
            f"🔜 <b>Первая пара (№{first_block['pair_number']}, {first_block['start_str']}-{first_block['end_str']}):</b>\n"
            f"{_format_pair_details(first_block)}"
        )

    # 2. После окончания всех пар
    if current_dt > last_block["end_dt"]:
        return (
            f"🎉 <b>Все пары на сегодня завершились!</b>\n"
            f"👥 Группа: <code>{group_name}</code>\n\n"
            f"👏 Учебный день окончен, хорошего отдыха!\n"
            f"💡 <i>Нажмите «📆 На завтра», чтобы посмотреть следующее расписание.</i>"
        )

    # 3. Проверяем, идет ли сейчас пара или перемена
    for idx, block in enumerate(pair_blocks):
        if block["start_dt"] <= current_dt <= block["end_dt"]:
            mins_left = max(1, int((block["end_dt"] - current_dt).total_seconds() // 60))
            next_info = ""
            if idx + 1 < len(pair_blocks):
                next_b = pair_blocks[idx + 1]
                next_info = (
                    f"\n🔜 <b>Следующая пара (№{next_b['pair_number']}, {next_b['start_str']}-{next_b['end_str']}):</b>\n"
                    f"{_format_pair_details(next_b)}"
                )
            else:
                next_info = "\n🏁 <b>Это последняя пара на сегодня!</b>"

            return (
                f"🔴 <b>Сейчас идет пара №{block['pair_number']} ({block['start_str']}-{block['end_str']})</b>\n"
                f"👥 Группа: <code>{group_name}</code>\n"
                f"⏳ До конца пары: <b>{mins_left} мин.</b> (звонок в {block['end_str']})\n\n"
                f"{_format_pair_details(block)}\n"
                f"{next_info}"
            )

        if idx + 1 < len(pair_blocks):
            next_b = pair_blocks[idx + 1]
            if block["end_dt"] < current_dt < next_b["start_dt"]:
                mins_left = max(1, int((next_b["start_dt"] - current_dt).total_seconds() // 60))
                break_duration = max(1, int((next_b["start_dt"] - block["end_dt"]).total_seconds() // 60))

                return (
                    f"☕ <b>Сейчас перемена!</b> (длительность {break_duration} мин.)\n"
                    f"👥 Группа: <code>{group_name}</code>\n"
                    f"⏳ До звонка на пару осталось: <b>{mins_left} мин.</b> (начало в {next_b['start_str']})\n\n"
                    f"🔜 <b>Куда идти (пара №{next_b['pair_number']}, {next_b['start_str']}-{next_b['end_str']}):</b>\n"
                    f"{_format_pair_details(next_b)}"
                )

    return format_day_schedule_message(sched, group_name)


def generate_ics_calendar(
    week_sched: list[dict[str, Any]], group_name: str
) -> str:
    """
    Генерирует RFC 5545 iCalendar файл (.ics) для расписания группы на неделю.
    Совместим с Apple Calendar (iOS), Google Calendar (Android/Web), Yandex и Outlook.
    """
    def _esc(val: str) -> str:
        if not val:
            return ""
        return (
            str(val).replace("\\", "\\\\")
            .replace(";", "\\;")
            .replace(",", "\\,")
            .replace("\n", "\\n")
        )

    now_utc_str = datetime.now(ZoneInfo("UTC")).strftime("%Y%m%dT%H%M%SZ")

    lines = [
        "BEGIN:VCALENDAR",
        "VERSION:2.0",
        "PRODID:-//KTMU Schedule Bot//RU",
        "CALSCALE:GREGORIAN",
        "METHOD:PUBLISH",
        f"X-WR-CALNAME:{_esc(f'Расписание {group_name}')}",
        "X-WR-TIMEZONE:Europe/Moscow",
    ]

    for day in week_sched:
        date_str = day.get("date", "")
        if not date_str:
            continue
        try:
            d_val = datetime.strptime(date_str, "%Y-%m-%d").date()
        except Exception:
            continue

        for lesson in day.get("lessons", []):
            subj = lesson.get("subject", "Занятие")
            room = lesson.get("room", "")
            tch = lesson.get("teacher", "")
            fmt = lesson.get("format", "Очно")
            pair_num = lesson.get("pair_number", 1)
            sub = lesson.get("subgroup", 0)
            is_ext = lesson.get("is_external", False)
            loc = lesson.get("location", "")

            start_t_str = lesson.get("start_time", "")
            end_t_str = lesson.get("end_time", "")
            if not start_t_str or not end_t_str:
                raw_t = lesson.get("time", "")
                if "-" in raw_t:
                    parts = raw_t.split("-")
                    start_t_str = parts[0].strip()
                    end_t_str = parts[1].strip()

            try:
                s_parts = start_t_str.split(":")
                e_parts = end_t_str.split(":")
                s_time = time(int(s_parts[0]), int(s_parts[1]))
                e_time = time(int(e_parts[0]), int(e_parts[1]))
            except Exception:
                continue

            dtstart = f"{d_val.strftime('%Y%m%d')}T{s_time.strftime('%H%M%S')}"
            dtend = f"{d_val.strftime('%Y%m%d')}T{e_time.strftime('%H%M%S')}"

            sub_txt = f" (подгр. {sub})" if sub else ""
            summary = f"{subj}{sub_txt}"
            if room and room != "—":
                summary += f" [{room}]"

            address = "Вознесенский пр., 44" if (is_ext or "Вознесенск" in loc) else (loc or "наб. реки Мойки, 61")
            location = f"каб. {room}, {address}" if room and room != "—" else address

            desc_parts = [
                f"Пара №{pair_num} ({start_t_str}-{end_t_str})",
                f"Группа: {group_name}",
            ]
            if tch and tch != "—":
                desc_parts.append(f"Преподаватель: {tch}")
            if fmt:
                desc_parts.append(f"Формат: {fmt}")

            description = "\n".join(desc_parts)
            clean_grp = group_name.replace(" ", "_")
            uid = f"{date_str}-p{pair_num}-s{sub}-{clean_grp}@ktmu-schedule"

            lines.extend([
                "BEGIN:VEVENT",
                f"UID:{uid}",
                f"DTSTAMP:{now_utc_str}",
                f"DTSTART;TZID=Europe/Moscow:{dtstart}",
                f"DTEND;TZID=Europe/Moscow:{dtend}",
                f"SUMMARY:{_esc(summary)}",
                f"LOCATION:{_esc(location)}",
                f"DESCRIPTION:{_esc(description)}",
                "STATUS:CONFIRMED",
                "END:VEVENT",
            ])

    lines.append("END:VCALENDAR")
    return "\r\n".join(lines) + "\r\n"


# Глобальный экземпляр парсера расписания
timetable_parser = TimetableParser()

