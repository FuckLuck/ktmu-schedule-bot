import asyncio
import os
import pytest
import pytest_asyncio
from datetime import date, datetime, timedelta
from unittest.mock import AsyncMock, MagicMock, patch

from aiogram import Bot
from aiogram.exceptions import TelegramForbiddenError
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, Message

from config import Settings
from database import Database
from keyboards import (
    GroupCallback,
    SpecialtyCallback,
    get_groups_inline_keyboard,
    get_main_reply_keyboard,
    get_specialties_inline_keyboard,
)
from scheduler import NotificationScheduler
from timetable_parser import (
    TimetableParser,
    format_day_schedule_message,
    format_pair_notification,
    format_week_schedule_messages,
)


@pytest_asyncio.fixture
async def test_db(tmp_path):
    """Изолированная тестовая база данных SQLite."""
    db_file = str(tmp_path / "test_ktmu.db")
    db = Database(db_path=db_file)
    await db.init_db()
    return db


@pytest.mark.asyncio
async def test_database_user_flow(test_db):
    """Тест создания пользователя, обновления группы и переключения уведомлений."""
    # 1. Новый пользователь
    user = await test_db.get_user(12345)
    assert user is None

    # 2. Добавление пользователя с группой
    await test_db.upsert_user(
        user_id=12345,
        group_id="grp_1",
        group_url="https://timetable-ktmu.ru/specialty/1/year/2026/group/grp_1",
        group_name="1-ИС-1",
        notifications_enabled=True,
    )

    user = await test_db.get_user(12345)
    assert user is not None
    assert user["user_id"] == 12345
    assert user["group_name"] == "1-ИС-1"
    assert user["notifications_enabled"] is True

    # 3. Переключение уведомлений
    new_status = await test_db.toggle_notifications(12345)
    assert new_status is False
    user = await test_db.get_user(12345)
    assert user["notifications_enabled"] is False

    # 4. Принудительное включение
    await test_db.set_user_notifications(12345, True)
    user = await test_db.get_user(12345)
    assert user["notifications_enabled"] is True


@pytest.mark.asyncio
async def test_database_groups_and_cache(test_db):
    """Тест сохранения групп, поиска по специальности и кэширования расписания."""
    groups_data = [
        {"id": "g1", "specialty_name": "Информатика", "group_name": "ИС-1", "relative_url": "/g1"},
        {"id": "g2", "specialty_name": "Информатика", "group_name": "ИС-2", "relative_url": "/g2"},
        {"id": "g3", "specialty_name": "Дизайн", "group_name": "Д-1", "relative_url": "/g3"},
    ]
    await test_db.upsert_groups_bulk(groups_data)

    assert await test_db.count_groups() == 3

    specs = await test_db.get_all_specialties()
    assert specs == ["Дизайн", "Информатика"]

    inf_groups = await test_db.get_groups_by_specialty("Информатика")
    assert len(inf_groups) == 2
    assert {g["group_name"] for g in inf_groups} == {"ИС-1", "ИС-2"}

    # Проверка кэширования
    test_sched = {"date": "2026-09-10", "lessons": [{"subject": "Математика"}]}
    await test_db.save_cached_timetable("g1", "2026-09-10", test_sched)

    cached = await test_db.get_cached_timetable("g1", "2026-09-10")
    assert cached is not None
    assert cached["lessons"][0]["subject"] == "Математика"

    # Несуществующий кэш
    none_cached = await test_db.get_cached_timetable("g1", "2026-09-11")
    assert none_cached is None


@pytest.mark.asyncio
async def test_active_users_grouping(test_db):
    """Тест группировки пользователей по group_id для рассылки в 20:00."""
    await test_db.upsert_user(101, "grp_A", "/urlA", "Группа А", notifications_enabled=True)
    await test_db.upsert_user(102, "grp_A", "/urlA", "Группа А", notifications_enabled=True)
    await test_db.upsert_user(103, "grp_B", "/urlB", "Группа Б", notifications_enabled=True)
    await test_db.upsert_user(104, "grp_B", "/urlB", "Группа Б", notifications_enabled=False)  # отключены

    grouped = await test_db.get_active_users_grouped_by_group()
    assert len(grouped["grp_A"]) == 2
    assert set(grouped["grp_A"]) == {101, 102}
    assert len(grouped["grp_B"]) == 1
    assert grouped["grp_B"] == [103]


def test_timetable_formatting():
    """Тест форматирования расписания на день, неделю и уведомлений о начале пары."""
    sample_day = {
        "date": "2026-09-10",
        "day_name": "Четверг",
        "week_number": 2,
        "is_even_week": True,
        "lessons": [
            {
                "pair_number": 1,
                "time": "08:30 - 10:00",
                "start_time": "08:30",
                "end_time": "10:00",
                "subject": "Информационные технологии",
                "teacher": "Иванов И.И.",
                "room": "101",
                "format": "Очно",
                "subgroup": 0,
            }
        ],
    }

    # 1. Форматирование дня
    msg = format_day_schedule_message(sample_day, "1-ИС-10")
    assert "Расписание на Четверг" in msg
    assert "1-ИС-10" in msg
    assert "Информационные технологии" in msg
    assert "101" in msg
    assert "Иванов И.И." in msg

    # 2. Форматирование пустого дня
    empty_day = {"date": "2026-09-13", "day_name": "Воскресенье", "week_number": 2, "lessons": []}
    empty_msg = format_day_schedule_message(empty_day, "1-ИС-10")
    assert "пар нет" in empty_msg

    # 3. Форматирование уведомления о начале пары
    notif_msg = format_pair_notification(sample_day["lessons"][0], "1-ИС-10")
    assert "Началась 1 пара!" in notif_msg
    assert "08:30 - 10:00" in notif_msg

    # 4. Форматирование недели
    week_msgs = format_week_schedule_messages([sample_day, empty_day], "1-ИС-10")
    assert len(week_msgs) >= 1
    assert "Расписание на неделю" in week_msgs[0]


from keyboards import (
    GroupCallback,
    SpecialtyCallback,
    get_groups_inline_keyboard,
    get_help_inline_keyboard,
    get_main_reply_keyboard,
    get_schedule_nav_keyboard,
    get_specialties_inline_keyboard,
)
from handlers import are_today_pairs_finished, get_help_section_text


def test_are_today_pairs_finished():
    """Тест определения окончания пар: если сейчас 16:00, а пара кончилась в 15:30 -> True."""
    sample_lessons = [
        {"pair_number": 1, "start_time": "08:30", "end_time": "10:00"},
        {"pair_number": 2, "start_time": "10:10", "end_time": "11:40"},
        {"pair_number": 3, "start_time": "11:50", "end_time": "13:20"},
        {"pair_number": 4, "start_time": "14:00", "end_time": "15:30"},
    ]

    # Ситуация 1: Сейчас 16:00 (пары закончились в 15:30) -> пары завершены
    dt_after = datetime(2026, 9, 9, 16, 0, 0)
    assert are_today_pairs_finished(sample_lessons, dt_after) is True

    # Ситуация 2: Сейчас 14:30 (пара еще идет) -> пары НЕ завершены
    dt_during = datetime(2026, 9, 9, 14, 30, 0)
    assert are_today_pairs_finished(sample_lessons, dt_during) is False

    # Ситуация 3: Выходной (пар нет вообще) -> считаем завершенными
    assert are_today_pairs_finished([], dt_after) is True


def test_keyboards_builder():
    """Тест генерации клавиатур: без кнопки «Вперед» и с кнопками «Назад» везде."""
    # 1. Специальности: выводятся все без разбивки на страницы и без кнопки «Вперед»
    specs = [f"Спец {i}" for i in range(10)]
    kb = get_specialties_inline_keyboard(specs, show_back_to_menu=True)
    assert kb.inline_keyboard is not None

    all_buttons = [btn.text for row in kb.inline_keyboard for btn in row]
    # ПРОВЕРКА: кнопки «Вперед» НЕТ!
    assert not any("Вперед" in text for text in all_buttons)
    # ПРОВЕРКА: кнопка «Назад в меню» ЕСТЬ!
    assert any("Назад" in text for text in all_buttons)

    # 2. Группы: кнопки групп + кнопки «Назад к специальностям» и «В главное меню»
    groups = [
        {"id": "1", "group_name": "1-ИС-1"},
        {"id": "2", "group_name": "1-ИС-2"},
    ]
    kb_grp = get_groups_inline_keyboard(groups)
    grp_buttons = [btn.text for row in kb_grp.inline_keyboard for btn in row]
    assert any("Назад к специальностям" in text for text in grp_buttons)
    assert any("В главное меню" in text for text in grp_buttons)

    # 3. Инлайн-кнопки навигации по расписанию
    kb_sched = get_schedule_nav_keyboard(current_date_str="2026-09-09", show_today_past=True)
    sched_buttons = [btn.text for row in kb_sched.inline_keyboard for btn in row]
    assert any("Показать прошедшее за сегодня" in text for text in sched_buttons)
    assert any("Назад в меню" in text for text in sched_buttons)

    # 4. Главное меню и Меню группы
    from keyboards import get_group_menu_keyboard, get_settings_info_keyboard
    reply_kb = get_main_reply_keyboard(notifications_enabled=True)
    assert len(reply_kb.keyboard) == 5
    assert any("Меню группы" in btn.text for row in reply_kb.keyboard for btn in row)
    assert any("На сегодня" in btn.text for row in reply_kb.keyboard for btn in row)
    assert any("Звонки" in btn.text for row in reply_kb.keyboard for btn in row)
    assert not any("Корпуса и кабинеты" in btn.text for row in reply_kb.keyboard for btn in row)
    assert any("ВКЛ" in btn.text for row in reply_kb.keyboard for btn in row)
    assert any("Поиск преподавателя" in btn.text for row in reply_kb.keyboard for btn in row)
    assert any("Настройки и связь" in btn.text for row in reply_kb.keyboard for btn in row)

    # Меню настроек и связи
    settings_kb = get_settings_info_keyboard(lang="ru")
    assert any("Язык" in btn.text for row in settings_kb.keyboard for btn in row)
    assert any("Инструкция" in btn.text for row in settings_kb.keyboard for btn in row)
    assert any("Связь с автором" in btn.text for row in settings_kb.keyboard for btn in row)
    assert any("Бот в группу" in btn.text for row in settings_kb.keyboard for btn in row)

    # 5. Отдельная клавиатура группы
    grp_menu_kb = get_group_menu_keyboard(lang="ru")
    assert len(grp_menu_kb.keyboard) == 4
    assert any("ДЗ" in btn.text for row in grp_menu_kb.keyboard for btn in row)
    assert any("Чат группы" in btn.text for row in grp_menu_kb.keyboard for btn in row)
    assert any("Личные заметки" in btn.text for row in grp_menu_kb.keyboard for btn in row)
    assert any("Староста" in btn.text for row in grp_menu_kb.keyboard for btn in row)
    assert any("Сменить группу" in btn.text for row in grp_menu_kb.keyboard for btn in row)
    assert any("Главное меню" in btn.text for row in grp_menu_kb.keyboard for btn in row)


@pytest.mark.asyncio
async def test_scheduler_telegram_forbidden_handling(test_db):
    """Тест отключения уведомлений в БД при перехвате TelegramForbiddenError."""
    # Добавляем пользователя с включенными уведомлениями
    await test_db.upsert_user(999, "grp_1", "/grp1", "1-ИС-1", notifications_enabled=True)

    mock_bot = MagicMock()
    # Имитируем ошибку блокировки бота пользователем
    mock_bot.send_message = AsyncMock(side_effect=TelegramForbiddenError(
        method=MagicMock(), message="Forbidden: bot was blocked by the user"
    ))

    scheduler = NotificationScheduler(bot=mock_bot, database=test_db)
    success = await scheduler._safe_send_message(999, "Тестовое сообщение")

    # Сообщение не должно быть доставлено
    assert success is False

    # В базе данных флаг notifications_enabled должен стать False
    user = await test_db.get_user(999)
    assert user["notifications_enabled"] is False


@pytest.mark.asyncio
async def test_scheduler_20_00_broadcast_grouping(test_db):
    """Тест: в 20:00 бот делает ровно 1 парсинг на группу, независимо от числа студентов."""
    # Создаем тестовую группу
    await test_db.upsert_groups_bulk([
        {"id": "grp_alpha", "specialty_name": "Спец", "group_name": "Альфа-1", "relative_url": "/alpha"}
    ])
    # Добавляем 5 студентов в одну группу
    for uid in [1, 2, 3, 4, 5]:
        await test_db.upsert_user(uid, "grp_alpha", "/alpha", "Альфа-1", notifications_enabled=True)

    mock_bot = MagicMock()
    mock_bot.send_message = AsyncMock(return_value=True)

    scheduler = NotificationScheduler(bot=mock_bot, database=test_db)

    # Мокаем fetch_day_schedule
    with patch("scheduler.timetable_parser.fetch_day_schedule", new_callable=AsyncMock) as mock_fetch:
        mock_fetch.return_value = {
            "date": "2026-09-11",
            "day_name": "Пятница",
            "week_number": 2,
            "is_even_week": True,
            "lessons": [],
        }

        await scheduler.broadcast_tomorrow_schedule()

        # КЛЮЧЕВАЯ ПРОВЕРКА: fetch_day_schedule вызван ровно 1 раз для всей группы!
        assert mock_fetch.call_count == 1
        # А сообщений отправлено 5 (каждому студенту)
        assert mock_bot.send_message.call_count == 5


@pytest.mark.asyncio
async def test_scheduler_07_00_pairs_creation(test_db):
    """Тест: в 07:00 планировщик создает точечные задачи на каждую пару дня."""
    await test_db.upsert_groups_bulk([
        {"id": "grp_beta", "specialty_name": "Спец", "group_name": "Бета-1", "relative_url": "/beta"}
    ])
    await test_db.upsert_user(777, "grp_beta", "/beta", "Бета-1", notifications_enabled=True)

    mock_bot = MagicMock()
    scheduler = NotificationScheduler(bot=mock_bot, database=test_db)

    mock_morning = datetime(2026, 9, 11, 7, 0, 0, tzinfo=scheduler.tz)
    future_time_str = "08:30"
    sample_sched = {
        "date": "2026-09-11",
        "day_name": "Пятница",
        "week_number": 1,
        "lessons": [
            {
                "pair_number": 1,
                "time": "08:30 - 10:00",
                "start_time": "08:30",
                "subject": "Спецпредмет",
                "room": "100",
                "teacher": "Профессор",
                "format": "Очно",
                "subgroup": 0,
            }
        ],
    }

    with patch("scheduler.datetime") as mock_dt, \
         patch.object(scheduler, "get_current_date", return_value=mock_morning.date()), \
         patch("scheduler.timetable_parser.fetch_day_schedule", new_callable=AsyncMock) as mock_fetch:
        mock_dt.now.return_value = mock_morning
        mock_dt.combine = datetime.combine
        mock_fetch.return_value = sample_sched

        await scheduler.schedule_today_pairs_notifications()

        # Проверяем, что в scheduler была добавлена точечная задача на пару
        jobs = scheduler.scheduler.get_jobs()
        pair_jobs = [j for j in jobs if j.id.startswith("pair_grp_beta")]
        assert len(pair_jobs) == 7
        exact_jobs = [j for j in pair_jobs if not "_lead_" in j.id]
        assert len(exact_jobs) == 1
        assert exact_jobs[0].kwargs["group_id"] == "grp_beta"
        assert exact_jobs[0].kwargs["group_name"] == "Бета-1"


@pytest.mark.asyncio
async def test_throttling_middleware_messages():
    """Тест антиспам-системы для входящих сообщений (Message)."""
    from throttling import ThrottlingMiddleware
    from aiogram.types import Message, User

    middleware = ThrottlingMiddleware(rate_limit=0.3)
    handler = AsyncMock(return_value="OK")

    user = User(id=123, is_bot=False, first_name="Тестер")
    message = MagicMock(spec=Message)
    message.from_user = user
    message.answer = AsyncMock()

    data = {"event_from_user": user}

    # 1. Первый запрос проходит успешно
    res1 = await middleware(handler, message, data)
    assert res1 == "OK"
    assert handler.call_count == 1
    assert message.answer.call_count == 0

    # 2. Быстрый второй запрос блокируется и отправляет предупреждение
    res2 = await middleware(handler, message, data)
    assert res2 is None
    assert handler.call_count == 1  # хэндлер не вызывался повторно
    assert message.answer.call_count == 1
    assert "не спамьте" in message.answer.call_args[0][0]

    # 3. Быстрый третий запрос блокируется молча (защита от циклического спама ботом)
    res3 = await middleware(handler, message, data)
    assert res3 is None
    assert handler.call_count == 1
    assert message.answer.call_count == 1  # новых предупреждений не отправлялось

    # 4. Запрос после завершения кулдауна проходит штатно
    await asyncio.sleep(0.35)
    res4 = await middleware(handler, message, data)
    assert res4 == "OK"
    assert handler.call_count == 2


@pytest.mark.asyncio
async def test_throttling_middleware_callback_queries():
    """Тест антиспам-системы для нажатий на кнопки (CallbackQuery)."""
    from throttling import ThrottlingMiddleware
    from aiogram.types import CallbackQuery, User

    middleware = ThrottlingMiddleware(rate_limit=0.3)
    handler = AsyncMock(return_value="OK")

    user = User(id=456, is_bot=False, first_name="Тестер")
    callback = MagicMock(spec=CallbackQuery)
    callback.from_user = user
    callback.answer = AsyncMock()

    data = {"event_from_user": user}

    # 1. Первое нажатие проходит
    res1 = await middleware(handler, callback, data)
    assert res1 == "OK"
    assert handler.call_count == 1
    assert callback.answer.call_count == 0

    # 2. Быстрое повторное нажатие блокируется, всплывает мягкое уведомление
    res2 = await middleware(handler, callback, data)
    assert res2 is None
    assert handler.call_count == 1
    assert callback.answer.call_count == 1
    assert "Слишком частые" in callback.answer.call_args[0][0]


def test_throttling_stale_cleanup():
    """Тест автоматической очистки устаревших записей троттлинга."""
    from throttling import ThrottlingMiddleware

    middleware = ThrottlingMiddleware(rate_limit=0.5, cleanup_threshold=2)
    now = 1000.0
    middleware._last_request_time = {
        101: now - 700.0,  # старая запись (> 600 сек)
        102: now - 50.0,   # свежая запись
    }
    middleware._user_warned = {101: True, 102: False}

    middleware._cleanup_stale_records(now)

    assert 101 not in middleware._last_request_time
    assert 101 not in middleware._user_warned
    assert 102 in middleware._last_request_time
    assert 102 in middleware._user_warned


def test_admin_config_and_permissions():
    """Тест проверки прав администратора в config."""
    from config import Settings
    s = Settings(ADMIN_IDS_RAW="870396858, 999111")
    assert s.is_admin(870396858) is True
    assert s.is_admin(999111) is True
    assert s.is_admin(123456) is False


@pytest.mark.asyncio
async def test_database_admin_stats_and_profile(test_db):
    """Тест сбора админ-статистики, топа групп и профилей пользователей."""
    # Создаем тестовые группы
    await test_db.upsert_groups_bulk([
        {"id": "g1", "specialty_name": "Спец 1", "group_name": "1-ИС-1", "relative_url": "/g1"},
        {"id": "g2", "specialty_name": "Спец 1", "group_name": "1-ИС-2", "relative_url": "/g2"},
    ])

    # 1. Добавляем пользователей с username и first_name
    await test_db.upsert_user(
        user_id=870396858,
        group_id="g1",
        group_url="/g1",
        group_name="1-ИС-1",
        notifications_enabled=True,
        username="yapsychokid",
        first_name="Админ",
    )
    await test_db.upsert_user(
        user_id=1002,
        group_id="g1",
        group_url="/g1",
        group_name="1-ИС-1",
        notifications_enabled=False,
        username="student1",
        first_name="Студент 1",
    )
    await test_db.upsert_user(
        user_id=1003,
        group_id="g2",
        group_url="/g2",
        group_name="1-ИС-2",
        notifications_enabled=True,
        username="student2",
        first_name="Студент 2",
    )

    # 2. Проверяем get_user
    user = await test_db.get_user(870396858)
    assert user["username"] == "yapsychokid"
    assert user["first_name"] == "Админ"
    assert user["group_name"] == "1-ИС-1"

    # 3. touch_user
    await test_db.touch_user(1004, username="newbie", first_name="Новичок")
    new_user = await test_db.get_user(1004)
    assert new_user is not None
    assert new_user["username"] == "newbie"

    # 4. Проверяем get_admin_stats
    stats = await test_db.get_admin_stats()
    assert stats["total_users"] == 4
    assert stats["with_group"] == 3
    assert stats["notifications_on"] == 3  # 870396858, 1003, и 1004 (по умолчанию DEFAULT 1)
    assert stats["total_groups"] == 2

    # 5. Проверяем get_top_groups
    top = await test_db.get_top_groups()
    assert len(top) == 2
    assert top[0]["group_name"] == "1-ИС-1"
    assert top[0]["count"] == 2
    assert top[1]["group_name"] == "1-ИС-2"
    assert top[1]["count"] == 1

    # 6. Проверяем get_all_user_ids
    uids = await test_db.get_all_user_ids()
    assert set(uids) == {870396858, 1002, 1003, 1004}

    # 7. Проверяем get_active_group_ids
    active_grps = await test_db.get_active_group_ids()
    assert set(active_grps) == {"g1", "g2"}


@pytest.mark.asyncio
async def test_instant_timetable_ram_and_sqlite_cache(test_db):
    """Тест мгновенной отдачи расписания через RAM-кэш и SQLite."""
    from timetable_parser import TimetableParser
    parser = TimetableParser(database=test_db)

    test_date = date(2026, 9, 10)
    date_str = test_date.isoformat()
    mock_schedule = {"date": date_str, "lessons": [{"subject": "Физика", "pair_number": 1}]}

    # 1. Сохраняем в SQLite
    await test_db.save_cached_timetable("grp_test", date_str, mock_schedule)

    # 2. Первый запрос: должен подгрузить из SQLite и записать в RAM-кэш
    with patch.object(parser, "_parse_schedule_from_api") as mock_api:
        sched1 = await parser.fetch_day_schedule("grp_test", target_date=test_date)
        assert sched1["lessons"][0]["subject"] == "Физика"
        # Сетевой API не должен был вызываться!
        assert mock_api.call_count == 0
        # Проверяем, что в RAM-кэше появилась запись
        assert ("grp_test", date_str) in parser._ram_day_cache

    # 3. Второй запрос: должен мгновенно вернуться из RAM-кэша без обращения к БД
    with patch.object(test_db, "get_cached_timetable") as mock_db_cache:
        sched2 = await parser.fetch_day_schedule("grp_test", target_date=test_date)
        assert sched2["lessons"][0]["subject"] == "Физика"
        # Даже SQLite не вызывался!
        assert mock_db_cache.call_count == 0

    # 4. Очистка кэша
    parser.clear_ram_cache()
    assert ("grp_test", date_str) not in parser._ram_day_cache
    deleted = await test_db.clear_timetable_cache()
    assert deleted >= 1
    assert await test_db.get_cached_timetable("grp_test", date_str) is None


@pytest.mark.asyncio
async def test_admin_panel_security(test_db):
    """Тест защиты панели администратора от неавторизованных пользователей."""
    from handlers import cmd_admin_panel
    from aiogram.types import Message, User

    # 1. Не-администратор (ID: 111222)
    user_non_admin = User(id=111222, is_bot=False, first_name="Обычный юзер")
    msg_non_admin = MagicMock(spec=Message)
    msg_non_admin.from_user = user_non_admin
    msg_non_admin.answer = AsyncMock()
    mock_state = AsyncMock()

    await cmd_admin_panel(msg_non_admin, mock_state, database=test_db)
    assert "Доступ запрещен" in msg_non_admin.answer.call_args[0][0]

    # 2. Администратор (ID: 870396858)
    user_admin = User(id=870396858, is_bot=False, first_name="Админ")
    msg_admin = MagicMock(spec=Message)
    msg_admin.from_user = user_admin
    msg_admin.answer = AsyncMock()

    await cmd_admin_panel(msg_admin, mock_state, database=test_db)
    assert "Панель администратора" in msg_admin.answer.call_args[0][0]


@pytest.mark.asyncio
async def test_is_group_admin():
    """Тест проверки прав администратора в группах."""
    from handlers import is_group_admin
    mock_bot = MagicMock()

    # 1. Личный чат (chat_id > 0) -> всегда True
    assert await is_group_admin(mock_bot, chat_id=12345, user_id=12345) is True

    # 2. Групповой чат (chat_id < 0), статус creator/administrator -> True
    mock_admin_member = MagicMock()
    mock_admin_member.status = "administrator"
    mock_bot.get_chat_member = AsyncMock(return_value=mock_admin_member)
    assert await is_group_admin(mock_bot, chat_id=-100123, user_id=555) is True

    # 3. Обычный участник (status = "member") -> False
    mock_user_member = MagicMock()
    mock_user_member.status = "member"
    mock_bot.get_chat_member = AsyncMock(return_value=mock_user_member)
    assert await is_group_admin(mock_bot, chat_id=-100123, user_id=777) is False


@pytest.mark.asyncio
async def test_group_chat_workflow_and_stats(test_db):
    """Тест привязки группы колледжа к Telegram-чату и подсчета групповых чатов."""
    from handlers import _get_authorized_user
    from aiogram.types import Chat, Message, User

    # 1. Чат без привязки
    group_chat = Chat(id=-100999888, type="supergroup", title="Чат 1-ИС-10")
    user = User(id=123, is_bot=False, first_name="Студент")
    msg_unbound = MagicMock(spec=Message)
    msg_unbound.chat = group_chat
    msg_unbound.from_user = user
    msg_unbound.answer = AsyncMock()

    auth_unbound = await _get_authorized_user(msg_unbound, test_db)
    assert auth_unbound is None
    assert "еще не выбрана учебная группа" in msg_unbound.answer.call_args[0][0]

    # 2. Привязываем группу колледжа к Telegram-чату
    await test_db.upsert_user(
        user_id=-100999888,
        group_id="grp_10",
        group_url="/grp10",
        group_name="1-ИС-10",
        notifications_enabled=True,
        first_name="Чат 1-ИС-10",
    )

    auth_bound = await _get_authorized_user(msg_unbound, test_db)
    assert auth_bound is not None
    assert auth_bound["group_id"] == "grp_10"
    assert auth_bound["group_name"] == "1-ИС-10"

    # 3. Проверяем статистику: разделение на private_users и group_chats
    await test_db.upsert_user(
        user_id=456,
        group_id="grp_10",
        group_url="/grp10",
        group_name="1-ИС-10",
        notifications_enabled=True,
        username="student_private",
    )
    stats = await test_db.get_admin_stats()
    assert stats["group_chats"] >= 1
    assert stats["private_users"] >= 1


@pytest.mark.asyncio
async def test_my_chat_member_event(test_db):
    """Тест приветственного сообщения при добавлении бота в группу."""
    from handlers import on_my_chat_member_updated
    from aiogram.types import Chat, ChatMemberAdministrator, ChatMemberUpdated

    mock_bot = MagicMock()
    mock_bot.send_message = AsyncMock()

    event = MagicMock(spec=ChatMemberUpdated)
    event.bot = mock_bot
    event.chat = Chat(id=-100555666, type="supergroup", title="Классная беседа")
    event.new_chat_member = MagicMock(spec=ChatMemberAdministrator)
    event.new_chat_member.status = "administrator"

    await test_db.upsert_groups_bulk([
        {"id": "g1", "specialty_name": "Информатика", "group_name": "1-ИС-1", "relative_url": "/g1"}
    ])

    await on_my_chat_member_updated(event, database=test_db)
    assert mock_bot.send_message.call_count == 1
    call_args = mock_bot.send_message.call_args
    assert call_args.kwargs["chat_id"] == -100555666
    assert "08:00" in call_args.kwargs["text"]


@pytest.mark.asyncio
async def test_help_system():
    """Тест интерактивной системы справки и инструкции."""
    from handlers import cb_help_section, cmd_help
    from keyboards import HelpCallback

    # 1. Проверка генерации текстов для всех секций
    for sec in ["main", "student", "group", "commands"]:
        text = get_help_section_text(sec)
        assert len(text) > 50
        assert "yapsychokid" in text or "КТМУ" in text

    assert "08:00" in get_help_section_text("group")
    assert "/set_group" in get_help_section_text("group")
    assert "прошедшее за сегодня" in get_help_section_text("student")

    # 2. Проверка инлайн-клавиатуры справки
    kb_main = get_help_inline_keyboard("main", show_back_to_menu=True)
    btns = [btn.text for row in kb_main.inline_keyboard for btn in row]
    assert any("Для студента" in b for b in btns)
    assert any("Добавить в беседу" in b for b in btns)
    assert any("Связь с автором" in b for b in btns)
    assert any("В главное меню" in b for b in btns)

    # 3. Тест обработчика команды cmd_help
    mock_msg = MagicMock()
    mock_msg.chat.type = "private"
    mock_msg.answer = AsyncMock()
    await cmd_help(mock_msg)
    assert mock_msg.answer.call_count == 1
    call_text = mock_msg.answer.call_args[0][0]
    assert "Как пользоваться ботом" in call_text

    # 4. Тест callback переключения секции
    mock_cb = MagicMock()
    mock_cb.message.chat.type = "private"
    mock_cb.message.edit_text = AsyncMock()
    mock_cb.answer = AsyncMock()
    cb_data = HelpCallback(section="group")
    await cb_help_section(mock_cb, cb_data)
    assert mock_cb.message.edit_text.call_count == 1
    assert "Как добавить бота в группу" in mock_cb.message.edit_text.call_args[0][0]


@pytest.mark.asyncio
async def test_chats_table_crud_and_topics(test_db):
    """Тест работы таблицы chats для супергрупп и топиков форума."""
    # 1. Добавление чата с конкретным топиком (message_thread_id=42)
    await test_db.upsert_chat(
        chat_id=-100111222,
        message_thread_id=42,
        group_id="grp_topic_1",
        group_url="/grp_topic_1",
        group_name="1-ИС-1",
        notifications_enabled=True,
    )

    # 2. Добавление общего чата без топика (message_thread_id=None)
    await test_db.upsert_chat(
        chat_id=-100111222,
        message_thread_id=None,
        group_id="grp_main",
        group_url="/grp_main",
        group_name="Общий-1",
        notifications_enabled=True,
    )

    # 3. Проверка get_chat
    chat_topic = await test_db.get_chat(-100111222, 42)
    assert chat_topic is not None
    assert chat_topic["group_id"] == "grp_topic_1"
    assert chat_topic["message_thread_id"] == 42

    chat_general = await test_db.get_chat(-100111222, None)
    assert chat_general is not None
    assert chat_general["group_id"] == "grp_main"
    assert chat_general["message_thread_id"] is None

    # 4. Группировка активных чатов по группам для рассылки
    grouped = await test_db.get_active_chats_grouped_by_group()
    assert "grp_topic_1" in grouped
    assert len(grouped["grp_topic_1"]) == 1
    assert grouped["grp_topic_1"][0] == (-100111222, 42)

    assert "grp_main" in grouped
    assert grouped["grp_main"][0] == (-100111222, None)

    # 5. Отключение уведомлений для топика
    await test_db.set_chat_notifications(-100111222, 42, False)
    chat_topic_updated = await test_db.get_chat(-100111222, 42)
    assert chat_topic_updated["notifications_enabled"] is False

    grouped_after = await test_db.get_active_chats_grouped_by_group()
    assert "grp_topic_1" not in grouped_after

    # 6. Удаление чата
    await test_db.delete_chat(-100111222, 42)
    assert await test_db.get_chat(-100111222, 42) is None
    # Общий чат должен остаться нетронутым
    assert await test_db.get_chat(-100111222, None) is not None


def test_subgroup_tree_formatting():
    """Тест древовидной группировки подгрупп (├──, └──) без дублирования заголовка пары."""
    day_data = {
        "date": "2026-09-12",
        "day_name": "Понедельник",
        "week_number": 1,
        "is_even_week": False,
        "lessons": [
            # 1 пара: общая для всей группы
            {
                "pair_number": 1,
                "time": "08:30 - 10:00",
                "start_time": "08:30",
                "end_time": "10:00",
                "subject": "История",
                "teacher": "Сидоров С.С.",
                "room": "201",
                "format": "Очно",
                "subgroup": 0,
            },
            # 2 пара: разделение на 2 подгруппы
            {
                "pair_number": 2,
                "time": "10:10 - 11:40",
                "start_time": "10:10",
                "end_time": "11:40",
                "subject": "Информатика",
                "teacher": "Преподаватель А",
                "room": "101",
                "format": "Очно",
                "subgroup": 1,
            },
            {
                "pair_number": 2,
                "time": "10:10 - 11:40",
                "start_time": "10:10",
                "end_time": "11:40",
                "subject": "Информатика",
                "teacher": "Преподаватель Б",
                "room": "102",
                "format": "Очно",
                "subgroup": 2,
            },
        ],
    }

    msg = format_day_schedule_message(day_data, "1-ИС-20")

    # Проверка общей пары
    assert "1 пара" in msg
    assert "08:30 - 10:00" in msg
    assert "История" in msg
    assert "Сидоров С.С." in msg

    # Проверка группировки подгрупп
    assert "2 пара" in msg
    assert "10:10 - 11:40" in msg
    assert msg.count("2 пара") == 1  # Заголовок пары НЕ дублируется!
    assert "Информатика" in msg
    assert "├── 👥 1 подгруппа: Ауд. 101 | 👤 Преподаватель А" in msg
    assert "└── 👥 2 подгруппа: Ауд. 102 | 👤 Преподаватель Б" in msg


@pytest.mark.asyncio
async def test_find_teacher_schedule_and_search(test_db):
    """Тест поиска расписания преподавателя по базе кэша."""
    # Сохраняем расписание для двух разных групп
    today_str = date.today().isoformat()
    tomorrow_str = (date.today() + timedelta(days=1)).isoformat()

    sched_grp1 = {
        "date": today_str,
        "lessons": [
            {
                "pair_number": 2,
                "time": "10:10 - 11:40",
                "subject": "Базы данных",
                "teacher": "Иванов Петр Сергеевич",
                "room": "305",
                "subgroup": 0,
            }
        ],
    }
    sched_grp2 = {
        "date": tomorrow_str,
        "lessons": [
            {
                "pair_number": 1,
                "time": "08:30 - 10:00",
                "subject": "Программирование",
                "teacher": "Иванов П.С.",
                "room": "204",
                "subgroup": 1,
            },
            {
                "pair_number": 3,
                "time": "11:50 - 13:20",
                "subject": "Английский язык",
                "teacher": "Смирнова А.В.",
                "room": "105",
                "subgroup": 0,
            }
        ],
    }

    await test_db.save_cached_timetable("grp_1", today_str, sched_grp1)
    await test_db.save_cached_timetable("grp_2", tomorrow_str, sched_grp2)

    # 1. Поиск "Иванов"
    results = await test_db.find_teacher_schedule("Иванов", min_date_str=today_str)
    assert len(results) == 2
    subjects = {r["subject"] for r in results}
    assert subjects == {"Базы данных", "Программирование"}
    assert results[0]["room"] in ("305", "204")

    # 2. Поиск "Смирнова"
    results_smirnova = await test_db.find_teacher_schedule("Смирнова", min_date_str=today_str)
    assert len(results_smirnova) == 1
    assert results_smirnova[0]["subject"] == "Английский язык"
    assert results_smirnova[0]["room"] == "105"

    # 3. Поиск несуществующего преподавателя
    results_none = await test_db.find_teacher_schedule("Несуществующий", min_date_str=today_str)
    assert len(results_none) == 0


@pytest.mark.asyncio
async def test_hash_calculation_and_substitutions(test_db):
    """Тест вычисления хэша расписания и мониторинга изменений/замен."""
    from timetable_parser import compute_schedule_hash

    lessons_v1 = [
        {"pair_number": 1, "subject": "Физика", "teacher": "Петров", "room": "101", "subgroup": 0}
    ]
    lessons_v2 = [
        {"pair_number": 1, "subject": "Математика (ЗАМЕНА)", "teacher": "Сидоров", "room": "102", "subgroup": 0}
    ]

    hash_v1 = compute_schedule_hash(lessons_v1)
    hash_v2 = compute_schedule_hash(lessons_v2)

    assert hash_v1 is not None
    assert hash_v2 is not None
    assert hash_v1 != hash_v2

    # Сохраняем в базу версию 1 с хэшем
    today_str = date.today().isoformat()
    sched_payload_v1 = {"date": today_str, "lessons": lessons_v1}
    await test_db.save_cached_timetable("grp_sub", today_str, sched_payload_v1, data_hash=hash_v1)

    cached_entry = await test_db.get_cached_timetable_entry("grp_sub", today_str)
    assert cached_entry is not None
    assert cached_entry["hash"] == hash_v1

    # Имитируем работу планировщика мониторинга замен
    mock_bot = MagicMock()
    mock_bot.send_message = AsyncMock(return_value=True)

    # Добавляем пользователя и чат-топик в группу
    await test_db.upsert_user(9991, "grp_sub", "/sub", "Тест-Группа", notifications_enabled=True)
    await test_db.upsert_chat(chat_id=-100999, message_thread_id=77, group_id="grp_sub", group_url="/sub", notifications_enabled=True)

    scheduler = NotificationScheduler(bot=mock_bot, database=test_db)

    # При возврате нового расписания (v2) с новым хэшем должно отправиться предупреждение
    sched_payload_v2 = {
        "date": today_str,
        "day_name": "Понедельник",
        "week_number": 1,
        "lessons": lessons_v2,
    }

    with patch("scheduler.timetable_parser.fetch_day_schedule", new_callable=AsyncMock) as mock_fetch:
        mock_fetch.return_value = sched_payload_v2

        await scheduler.check_timetable_substitutions_and_changes()

        # Проверяем, что бот отправил предупреждения об изменении расписания
        assert mock_bot.send_message.call_count == 2  # пользователю и в топик
        sent_texts = [call.kwargs["text"] for call in mock_bot.send_message.call_args_list]
        for text in sent_texts:
            assert "Изменение в расписании!" in text
            assert "Математика (ЗАМЕНА)" in text

        # Проверяем, что в топик было передано message_thread_id=77
        topic_call = [call for call in mock_bot.send_message.call_args_list if call.kwargs["chat_id"] == -100999]
        assert len(topic_call) == 1
        assert topic_call[0].kwargs["message_thread_id"] == 77


@pytest.mark.asyncio
async def test_group_help_and_set_topic_handlers(test_db):
    """Тест команды /group_help и команды /set_topic в супергруппах и личных сообщениях."""
    from handlers import cmd_group_help, cmd_set_topic
    from aiogram.types import Chat, Message, User

    # 1. /group_help в личном чате
    mock_user = User(id=123, is_bot=False, first_name="Тестер")
    msg_help = MagicMock(spec=Message)
    msg_help.chat = Chat(id=123, type="private")
    msg_help.from_user = mock_user
    msg_help.answer = AsyncMock()

    await cmd_group_help(msg_help)
    assert msg_help.answer.call_count == 1
    help_text = msg_help.answer.call_args[0][0]
    assert "Как добавить бота в группу или тему" in help_text
    assert "/set_topic" in help_text
    assert "Администратора" in help_text

    # 2. /set_topic в личном чате (должно выдать предупреждение)
    msg_topic_private = MagicMock(spec=Message)
    msg_topic_private.chat = Chat(id=123, type="private")
    msg_topic_private.from_user = mock_user
    msg_topic_private.answer = AsyncMock()

    await cmd_set_topic(msg_topic_private, database=test_db)
    assert msg_topic_private.answer.call_count == 1
    assert "в групповых чатах и темах" in msg_topic_private.answer.call_args[0][0]

    # 3. /set_topic в супергруппе без прав админа
    mock_bot = MagicMock()
    msg_topic_group = MagicMock(spec=Message)
    msg_topic_group.bot = mock_bot
    msg_topic_group.chat = Chat(id=-100888999, type="supergroup", title="Беседа курса")
    msg_topic_group.from_user = mock_user
    msg_topic_group.message_thread_id = 55
    msg_topic_group.answer = AsyncMock()

    # Имитируем, что пользователь не админ
    with patch("handlers.is_group_admin", new_callable=AsyncMock) as mock_admin_check:
        mock_admin_check.return_value = False
        await cmd_set_topic(msg_topic_group, database=test_db)
        assert "только администраторы" in msg_topic_group.answer.call_args[0][0]

    # 4. /set_topic в супергруппе с правами админа
    await test_db.upsert_groups_bulk([
        {"id": "g1", "specialty_name": "Информатика", "group_name": "1-ИС-1", "relative_url": "/g1"}
    ])

    with patch("handlers.is_group_admin", new_callable=AsyncMock) as mock_admin_check:
        mock_admin_check.return_value = True
        await cmd_set_topic(msg_topic_group, database=test_db)
        assert msg_topic_group.answer.call_count == 2
        last_answer_text = msg_topic_group.answer.call_args_list[-1][0][0]
        assert "Выбор учебной группы для темы #55" in last_answer_text


def test_mobile_groups_keyboard_two_columns():
    """Тест верстки групп для мобильных устройств: ровно 2 колонки, без обрезки названий."""
    groups = [
        {"id": "1", "group_name": "1-КДД-10"},
        {"id": "2", "group_name": "1-КДД-12"},
        {"id": "3", "group_name": "1-КДД-14"},
        {"id": "4", "group_name": "1-КДД-18/1"},
    ]
    kb = get_groups_inline_keyboard(groups)
    assert kb.inline_keyboard is not None

    # Проверяем, что ряды с группами содержат ровно по 2 кнопки (не 3)
    group_rows = [row for row in kb.inline_keyboard if any(b.text.startswith("1-КДД") for b in row)]
    assert len(group_rows) == 2
    for r in group_rows:
        assert len(r) == 2
        for btn in r:
            # Названия чистые, без лишних символов
            assert not btn.text.startswith("🎓 ")
            assert "1-КДД" in btn.text


def test_bonchgo_schedule_navigation_keyboard():
    """Тест BonchGo-раскладки инлайн-навигации: дни, недели, картинка, вся неделя."""
    from keyboards import get_schedule_bonch_keyboard, ScheduleNavCallback

    target = date(2026, 9, 12)  # Суббота, 12 сентября 2026
    kb = get_schedule_bonch_keyboard(target, show_back_to_menu=True)

    rows = kb.inline_keyboard
    assert len(rows) == 4

    # Ряд 1: Вчера и Завтра
    r1 = rows[0]
    assert len(r1) == 2
    assert "11.09" in r1[0].text and "⬅️" in r1[0].text
    assert "13.09" in r1[1].text and "➡️" in r1[1].text

    # Ряд 2: Прошлая и Следующая неделя (-7 / +7 дней)
    r2 = rows[1]
    assert len(r2) == 2
    assert "05.09" in r2[0].text and "⏪" in r2[0].text
    assert "19.09" in r2[1].text and "⏩" in r2[1].text

    # Ряд 3: Картинка и Вся неделя
    r3 = rows[2]
    assert len(r3) == 2
    assert "Картинка" in r3[0].text
    assert "Вся неделя" in r3[1].text


def test_render_schedule_image():
    """Тест генерации графической PNG-карточки расписания."""
    from image_generator import render_schedule_image

    sample_day = {
        "date": "2026-09-12",
        "day_name": "Суббота",
        "week_number": 2,
        "is_even_week": True,
        "lessons": [
            {
                "pair_number": 1,
                "time": "08:30 - 10:00",
                "subject": "Информационные технологии",
                "room": "101",
                "teacher": "Иванов И.И.",
                "subgroup": 0,
            },
            {
                "pair_number": 2,
                "time": "10:10 - 11:40",
                "subject": "Базы данных",
                "room": "102",
                "teacher": "Петров П.П.",
                "subgroup": 1,
            },
            {
                "pair_number": 2,
                "time": "10:10 - 11:40",
                "subject": "Базы данных",
                "room": "103",
                "teacher": "Сидоров С.С.",
                "subgroup": 2,
            },
        ],
    }

    img_io = render_schedule_image(sample_day, "1-ИС-10", bot_username="test_ktmu_bot")
    assert img_io is not None
    data = img_io.getvalue()
    assert len(data) > 1000
    # Проверка магических байтов PNG-формата
    assert data.startswith(b"\x89PNG\r\n\x1a\n")


@pytest.mark.asyncio
async def test_teacher_search_by_subject(test_db):
    """Тест поиска преподавателей по предметам своей академической группы."""
    from keyboards import get_teacher_search_choice_keyboard, get_group_subjects_inline_keyboard

    # Заполняем кэш расписанием группы с разными предметами
    today_str = date.today().isoformat()
    sched = {
        "date": today_str,
        "day_name": "Понедельник",
        "lessons": [
            {
                "pair_number": 1,
                "time": "08:30 - 10:00",
                "subject": "Высшая математика",
                "teacher": "Архипова М.А.",
                "room": "301",
                "subgroup": 0,
            },
            {
                "pair_number": 2,
                "time": "10:10 - 11:40",
                "subject": "Информатика",
                "teacher": "Кузнецов К.К.",
                "room": "205",
                "subgroup": 1,
            },
            {
                "pair_number": 2,
                "time": "10:10 - 11:40",
                "subject": "Информатика",
                "teacher": "Лебедев Л.Л.",
                "room": "206",
                "subgroup": 2,
            },
        ],
    }
    await test_db.save_cached_timetable("grp_subject_test", today_str, sched)

    # 1. Извлекаем предметы группы из БД
    subj_dict = await test_db.get_group_subjects_and_teachers("grp_subject_test")
    assert len(subj_dict) == 2
    assert "Высшая математика" in subj_dict
    assert "Информатика" in subj_dict

    # Проверяем преподавателей математики
    math_info = subj_dict["Высшая математика"]
    assert "Архипова М.А." in math_info["teachers"]
    assert "301" in math_info["rooms"]

    # Проверяем преподавателей информатики (2 подгруппы)
    inf_info = subj_dict["Информатика"]
    assert len(inf_info["teachers"]) == 2
    assert any("Кузнецов" in t for t in inf_info["teachers"])
    assert any("Лебедев" in t for t in inf_info["teachers"])

    # 2. Проверяем клавиатуру выбора поиска
    choice_kb = get_teacher_search_choice_keyboard()
    choice_texts = [btn.text for row in choice_kb.inline_keyboard for btn in row]
    assert any("ФИО" in t for t in choice_texts)
    assert any("Преподаватели моей группы" in t for t in choice_texts)

    # 3. Проверяем клавиатуру предметов
    subjects_kb = get_group_subjects_inline_keyboard(list(subj_dict.keys()))
    subj_btn_texts = [btn.text for row in subjects_kb.inline_keyboard for btn in row]
    assert any("Высшая математика" in t for t in subj_btn_texts)
    assert any("Информатика" in t for t in subj_btn_texts)


def test_add_to_group_keyboard():
    """Тест генерации кнопки добавления бота в группу в 1 клик (startgroup=true)."""
    from keyboards import get_add_to_group_keyboard

    kb = get_add_to_group_keyboard("ktmu_schedule_bot")
    btns = [btn for row in kb.inline_keyboard for btn in row]
    url_btn = next((b for b in btns if b.url and "startgroup=true" in b.url), None)
    assert url_btn is not None
    assert "Добавить бота в беседу" in url_btn.text
    assert url_btn.url == "https://t.me/ktmu_schedule_bot?startgroup=true"


def test_i18n_translations():
    """Тест модуля интернационализации (RU / EN)."""
    from i18n import get_text

    # Русский язык
    ru_welcome = get_text("choose_language", "ru")
    assert "Добро пожаловать" in ru_welcome
    ru_hw = get_text("hw_broadcast_alert", "ru", group_name="ИТ-21", subject="Физика", due_date="15.09.2026", task_text="Стр 45", author="Иван")
    assert "Физика" in ru_hw
    assert "ИТ-21" in ru_hw

    # Английский язык
    en_welcome = get_text("choose_language", "en")
    assert "Welcome" in en_welcome
    en_btn = get_text("btn_hw", "en")
    assert "Homework" in en_btn


@pytest.mark.asyncio
async def test_database_language_and_lead_time(test_db):
    """Тест сохранения и получения языка пользователя и интервала напоминания."""
    user_id = 999111
    # По умолчанию язык 'ru' и lead time 0
    lang = await test_db.get_user_language(user_id)
    assert lang == "ru"
    lead = await test_db.get_user_lead_minutes(user_id)
    assert lead == 0

    # Устанавливаем язык 'en'
    await test_db.set_user_language(user_id, "en")
    assert await test_db.get_user_language(user_id) == "en"

    # Устанавливаем интервал 15 минут
    await test_db.set_user_lead_minutes(user_id, 15)
    assert await test_db.get_user_lead_minutes(user_id) == 15


@pytest.mark.asyncio
async def test_database_starosta_and_homework(test_db):
    """Тест CRUD операций старосты и домашних заданий."""
    group_id = "grp_test_starosta"

    # 1. Проверяем отсутствие старосты
    assert await test_db.get_group_starosta(group_id) is None

    # 2. Назначаем старосту
    await test_db.set_group_starosta(group_id, 12345, "ivan_starosta", "Иван Иванов")
    starosta = await test_db.get_group_starosta(group_id)
    assert starosta is not None
    assert starosta["user_id"] == 12345
    assert starosta["username"] == "ivan_starosta"
    assert starosta["full_name"] == "Иван Иванов"

    # 3. Добавляем домашнее задание
    hw1_id = await test_db.add_homework(
        group_id=group_id,
        subject="Высшая математика",
        due_date="15.09.2026",
        task_text="Номера 12, 14, 15",
        author_id=12345
    )
    hw2_id = await test_db.add_homework(
        group_id=group_id,
        subject="Информатика",
        due_date="16.09.2026",
        task_text="Лабораторная работа №1",
        author_id=12345
    )
    assert hw1_id > 0
    assert hw2_id > 0

    # 4. Проверяем получение списка ДЗ
    hw_list = await test_db.get_upcoming_homework(group_id)
    assert len(hw_list) == 2
    assert hw_list[0]["subject"] == "Высшая математика"
    assert hw_list[1]["subject"] == "Информатика"

    # 5. Проверяем получение по конкретной дате
    hw_by_date = await test_db.get_homework_for_date(group_id, "15.09.2026")
    assert len(hw_by_date) == 1
    assert hw_by_date[0]["subject"] == "Высшая математика"

    # 6. Удаляем ДЗ
    deleted = await test_db.delete_homework(hw1_id, group_id=group_id)
    assert deleted is True
    remaining = await test_db.get_upcoming_homework(group_id)
    assert len(remaining) == 1
    assert remaining[0]["id"] == hw2_id

    # 7. Складываем полномочия старосты
    resigned = await test_db.remove_group_starosta(group_id)
    assert resigned is True
    assert await test_db.get_group_starosta(group_id) is None


def test_keyboards_starosta_and_hw():
    """Тест генерации клавиатур старосты, ДЗ и выбора языка."""
    from keyboards import (
        get_language_inline_keyboard,
        get_notification_lead_time_keyboard,
        get_starosta_inline_keyboard,
        get_homework_list_keyboard,
        get_homework_delete_keyboard,
    )

    # 1. Выбор языка
    lang_kb = get_language_inline_keyboard()
    lang_texts = [btn.text for row in lang_kb.inline_keyboard for btn in row]
    assert any("Русский" in t for t in lang_texts)
    assert any("English" in t for t in lang_texts)

    # 2. Время напоминания (Bonch Bot style)
    lead_kb = get_notification_lead_time_keyboard("ru")
    lead_texts = [btn.text for row in lead_kb.inline_keyboard for btn in row]
    assert any("5 минут" in t for t in lead_texts)
    assert any("15 минут" in t for t in lead_texts)
    assert any("Не нужно" in t for t in lead_texts)

    # 3. Староста (когда старосты нет — заявка)
    no_starosta_kb = get_starosta_inline_keyboard(is_current_user=False, has_starosta=False)
    no_st_texts = [btn.text for row in no_starosta_kb.inline_keyboard for btn in row]
    assert any("Подать заявку на старосту" in t for t in no_st_texts)

    # 4. Староста (когда пользователь — староста: запрос на снятие)
    starosta_kb = get_starosta_inline_keyboard(is_current_user=True, has_starosta=True)
    st_texts = [btn.text for row in starosta_kb.inline_keyboard for btn in row]
    assert any("Добавить ДЗ" in t for t in st_texts)
    assert any("Удалить ДЗ" in t for t in st_texts)
    assert any("Запрос на снятие старосты" in t for t in st_texts)

    # 5. Список ДЗ для старосты
    hw_kb = get_homework_list_keyboard([{"id": 1, "subject": "Мат"}], is_starosta=True)
    hw_texts = [btn.text for row in hw_kb.inline_keyboard for btn in row]
    assert any("Добавить ДЗ" in t for t in hw_texts)
    assert any("Удалить ДЗ" in t for t in hw_texts)

    # 6. Клавиатура решения администратора
    from keyboards import get_admin_starosta_decision_keyboard
    admin_dec_kb = get_admin_starosta_decision_keyboard(1, "grp_test", 12345)
    admin_dec_texts = [btn.text for row in admin_dec_kb.inline_keyboard for btn in row]
    assert any("Одобрить" in t for t in admin_dec_texts)
    assert any("Отклонить" in t for t in admin_dec_texts)


@pytest.mark.asyncio
async def test_cmd_start_new_user_shows_language(test_db):
    """Тест: новый пользователь при /start видит выбор языка."""
    from handlers import cmd_start

    mock_msg = MagicMock(spec=Message)
    mock_msg.chat = MagicMock(type="private", id=555444)
    mock_msg.from_user = MagicMock(id=555444, username="newbie", first_name="Alex")
    mock_msg.answer = AsyncMock()

    await cmd_start(mock_msg, database=test_db)
    mock_msg.answer.assert_called_once()
    call_args = mock_msg.answer.call_args
    assert "Выберите язык" in call_args[0][0]
    assert call_args[1]["reply_markup"] is not None


@pytest.mark.asyncio
async def test_starosta_claim_and_homework_flow(test_db):
    """Интеграционный тест: студент становится старостой, добавляет ДЗ и бот его транслирует."""
    from handlers import (
        cmd_starosta,
        cb_claim_starosta,
        cmd_homework,
        process_hw_subject_text,
        process_hw_date_text,
        process_hw_task_text,
        HomeworkStates,
    )
    from keyboards import StarostaCallback

    group_id = "grp_flow_test"
    starosta_id = 111222
    student2_id = 333444

    # Настраиваем студентов в группе
    await test_db.upsert_groups_bulk([
        {"id": group_id, "specialty_name": "Программирование", "group_name": "ПР-21", "relative_url": "/pr21"}
    ])
    await test_db.upsert_user(starosta_id, group_id, "/pr21", "ПР-21", notifications_enabled=True, username="starosta_user", first_name="Староста")
    await test_db.upsert_user(student2_id, group_id, "/pr21", "ПР-21", notifications_enabled=True, username="student2", first_name="Студент")

    # 1. Проверяем /starosta (старосты нет -> предлагает стать)
    msg_st = MagicMock(spec=Message)
    msg_st.chat = MagicMock(type="private", id=starosta_id)
    msg_st.from_user = MagicMock(id=starosta_id, username="starosta_user", first_name="Староста")
    msg_st.answer = AsyncMock()

    await cmd_starosta(msg_st, database=test_db)
    assert "нет старосты" in msg_st.answer.call_args[0][0]

    # 2. Нажимаем «Подать заявку на старосту» -> отправляется заявка администратору
    mock_bot_claim = MagicMock(spec=Bot)
    mock_bot_claim.send_message = AsyncMock()

    cb_claim = MagicMock(spec=CallbackQuery)
    cb_claim.from_user = MagicMock(id=starosta_id, username="starosta_user", full_name="Иван Староста")
    cb_claim.message = MagicMock(spec=Message)
    cb_claim.message.edit_text = AsyncMock()
    cb_claim.answer = AsyncMock()
    cb_claim.bot = mock_bot_claim

    await cb_claim_starosta(cb_claim, database=test_db)
    # До подтверждения админом староста еще не назначен
    assert await test_db.get_group_starosta(group_id) is None
    assert "Заявка" in cb_claim.message.edit_text.call_args[0][0]
    assert mock_bot_claim.send_message.called

    # Проверяем заявку в базе данных
    pending_req = await test_db.get_pending_starosta_request(group_id, starosta_id, "claim")
    assert pending_req is not None
    req_id = pending_req["id"]

    # 2.5 Главный администратор бота одобряет заявку
    from handlers import cb_admin_starosta_decision
    from keyboards import AdminStarostaApproveCallback
    from config import config

    admin_id = config.ADMIN_IDS[0]
    cb_admin = MagicMock(spec=CallbackQuery)
    cb_admin.from_user = MagicMock(id=admin_id, full_name="Главный Администратор")
    cb_admin.message = MagicMock(spec=Message)
    cb_admin.message.edit_text = AsyncMock()
    cb_admin.answer = AsyncMock()
    cb_admin.bot = mock_bot_claim

    approve_data = AdminStarostaApproveCallback(
        action="approve", req_id=req_id, group_id=group_id, candidate_id=starosta_id
    )
    await cb_admin_starosta_decision(cb_admin, approve_data, database=test_db)

    # Теперь староста успешно назначен в базу!
    appointed = await test_db.get_group_starosta(group_id)
    assert appointed is not None
    assert appointed["user_id"] == starosta_id

    # 3. Добавление ДЗ через FSM:
    state = MagicMock(spec=FSMContext)
    fsm_data = {"group_id": group_id, "group_name": "ПР-21", "lang": "ru"}
    state.get_data = AsyncMock(return_value=fsm_data)
    state.update_data = AsyncMock(side_effect=lambda **kw: fsm_data.update(kw))
    state.set_state = AsyncMock()
    state.clear = AsyncMock()

    # Шаг 1: ввод предмета
    msg_subj = MagicMock(spec=Message)
    msg_subj.text = "Базы данных"
    msg_subj.answer = AsyncMock()
    await process_hw_subject_text(msg_subj, state)
    assert fsm_data["subject"] == "Базы данных"

    # Шаг 2: ввод срока сдачи
    msg_date = MagicMock(spec=Message)
    msg_date.text = "20.09.2026"
    msg_date.answer = AsyncMock()
    await process_hw_date_text(msg_date, state)
    assert fsm_data["due_date"] == "20.09.2026"

    # Шаг 3: ввод текста задания -> автотрансляция!
    mock_bot = MagicMock(spec=Bot)
    mock_bot.send_message = AsyncMock()

    msg_task = MagicMock(spec=Message)
    msg_task.from_user = MagicMock(id=starosta_id, full_name="Иван Староста", username="starosta_user")
    msg_task.text = "Сделать нормализацию до 3НФ"
    msg_task.bot = mock_bot
    msg_task.answer = AsyncMock()

    await process_hw_task_text(msg_task, state, database=test_db)

    # Проверяем сохранение в БД
    hw_entries = await test_db.get_upcoming_homework(group_id)
    assert len(hw_entries) == 1
    assert hw_entries[0]["subject"] == "Базы данных"
    assert hw_entries[0]["task_text"] == "Сделать нормализацию до 3НФ"

    # Проверяем, что бот автоматически транслировал ДЗ (вызов send_message для участников)
    assert mock_bot.send_message.call_count >= 2
    sent_texts = [call[1]["text"] for call in mock_bot.send_message.call_args_list]
    assert any("Новое домашнее задание" in t for t in sent_texts)
    assert any("Базы данных" in t for t in sent_texts)

    # 4. Проверяем просмотр ДЗ через кнопку «📚 ДЗ 📚»
    msg_hw = MagicMock(spec=Message)
    msg_hw.from_user = MagicMock(id=student2_id)
    msg_hw.answer = AsyncMock()
    await cmd_homework(msg_hw, database=test_db)
    hw_view_text = msg_hw.answer.call_args[0][0]
    assert "Базы данных" in hw_view_text
    assert "Сделать нормализацию" in hw_view_text


@pytest.mark.asyncio
async def test_scheduler_broadcast_includes_homework(test_db):
    """Тест: вечерняя рассылка в 20:00 автоматически прикрепляет домашнее задание на завтра."""
    group_id = "grp_hw_broadcast"
    tomorrow = date.today() + timedelta(days=1)
    tomorrow_str = tomorrow.strftime("%d.%m.%Y")

    await test_db.upsert_groups_bulk([
        {"id": group_id, "specialty_name": "ИТ", "group_name": "ИТ-11", "relative_url": "/it11"}
    ])
    await test_db.upsert_user(98765, group_id, "/it11", "ИТ-11", notifications_enabled=True)

    # Добавляем ДЗ на завтра
    await test_db.add_homework(
        group_id=group_id,
        subject="Английский язык",
        due_date=tomorrow_str,
        task_text="Выучить слова Unit 4",
        author_id=123
    )

    mock_bot = MagicMock(spec=Bot)
    mock_bot.send_message = AsyncMock()
    scheduler = NotificationScheduler(bot=mock_bot, database=test_db)

    sample_sched = {
        "date": tomorrow.isoformat(),
        "day_name": "Суббота",
        "week_number": 1,
        "lessons": [
            {
                "pair_number": 1,
                "time": "08:30 - 10:00",
                "start_time": "08:30",
                "subject": "Английский язык",
                "room": "401",
                "teacher": "Смит Дж.",
                "format": "очно",
                "subgroup": 0,
            }
        ],
    }

    with patch("scheduler.timetable_parser.fetch_day_schedule", new_callable=AsyncMock) as mock_fetch:
        mock_fetch.return_value = sample_sched
        await scheduler.broadcast_tomorrow_schedule()

    assert mock_bot.send_message.called
    sent_text = mock_bot.send_message.call_args[1]["text"]
    assert "Вечерняя рассылка" in sent_text
    assert "Домашнее задание на завтра" in sent_text
    assert "Выучить слова Unit 4" in sent_text


@pytest.mark.asyncio
async def test_cb_select_language_and_lead_time(test_db):
    """Тест переключения языка и выбора интервала уведомлений через CallbackQuery."""
    from handlers import cb_select_language, cb_select_lead_time
    from keyboards import LanguageCallback, LeadTimeCallback

    user_id = 888999
    await test_db.upsert_groups_bulk([
        {"id": "grp_lang", "specialty_name": "Дизайн", "group_name": "Д-11", "relative_url": "/d11"}
    ])

    # 1. Пользователь без группы выбирает русский язык -> бот показывает специальности
    cb_ru = MagicMock(spec=CallbackQuery)
    cb_ru.from_user = MagicMock(id=user_id)
    cb_ru.message = MagicMock(spec=Message)
    cb_ru.message.edit_text = AsyncMock()
    cb_ru.answer = AsyncMock()

    await cb_select_language(cb_ru, LanguageCallback(lang="ru"), database=test_db)
    assert await test_db.get_user_language(user_id) == "ru"
    assert "специальность" in cb_ru.message.edit_text.call_args[0][0]

    # 2. Привязываем группу пользователю и переключаем язык на английский
    await test_db.upsert_user(user_id, "grp_lang", "/d11", "Д-11", notifications_enabled=True)
    cb_en = MagicMock(spec=CallbackQuery)
    cb_en.from_user = MagicMock(id=user_id)
    cb_en.message = MagicMock(spec=Message)
    cb_en.message.delete = AsyncMock()
    cb_en.message.answer = AsyncMock()
    cb_en.answer = AsyncMock()

    await cb_select_language(cb_en, LanguageCallback(lang="en"), database=test_db)
    assert await test_db.get_user_language(user_id) == "en"
    assert "English" in cb_en.message.answer.call_args[0][0]

    # 3. Пользователь настраивает напоминание за 15 минут
    cb_lead = MagicMock(spec=CallbackQuery)
    cb_lead.from_user = MagicMock(id=user_id)
    cb_lead.message = MagicMock(spec=Message)
    cb_lead.message.delete = AsyncMock()
    cb_lead.message.answer = AsyncMock()
    cb_lead.answer = AsyncMock()

    await cb_select_lead_time(cb_lead, LeadTimeCallback(minutes=15), database=test_db)
    assert await test_db.get_user_lead_minutes(user_id) == 15
    assert "15 minutes" in cb_lead.message.answer.call_args[0][0]


@pytest.mark.asyncio
async def test_homework_deletion_and_starosta_resign(test_db):
    """Тест удаления домашнего задания старостой и сложения полномочий старосты."""
    from handlers import (
        cb_homework_delete_pick,
        cb_homework_delete_confirm,
        cb_homework_cancel,
        cb_resign_starosta,
    )
    from keyboards import HomeworkCallback, StarostaCallback

    group_id = "grp_del_test"
    starosta_id = 444555

    await test_db.upsert_groups_bulk([
        {"id": group_id, "specialty_name": "ИТ", "group_name": "ИТ-01", "relative_url": "/it01"}
    ])
    await test_db.upsert_user(starosta_id, group_id, "/it01", "ИТ-01", notifications_enabled=True)
    await test_db.set_group_starosta(group_id, starosta_id, "starosta_login", "Алексей")

    # Добавляем ДЗ
    hw_id = await test_db.add_homework(group_id, "История", "25.09.2026", "Эссе", starosta_id)
    assert len(await test_db.get_upcoming_homework(group_id)) == 1

    # 1. Открытие меню удаления
    cb_pick = MagicMock(spec=CallbackQuery)
    cb_pick.from_user = MagicMock(id=starosta_id)
    cb_pick.message = MagicMock(spec=Message)
    cb_pick.message.edit_text = AsyncMock()
    cb_pick.answer = AsyncMock()

    await cb_homework_delete_pick(cb_pick, database=test_db)
    assert "Выберите задание" in cb_pick.message.edit_text.call_args[0][0]

    # 2. Подтверждение удаления
    cb_del = MagicMock(spec=CallbackQuery)
    cb_del.from_user = MagicMock(id=starosta_id)
    cb_del.message = MagicMock(spec=Message)
    cb_del.message.edit_text = AsyncMock()
    cb_del.answer = AsyncMock()

    await cb_homework_delete_confirm(cb_del, HomeworkCallback(action="del", hw_id=hw_id), database=test_db)
    assert len(await test_db.get_upcoming_homework(group_id)) == 0
    assert "удалено" in cb_del.message.edit_text.call_args[0][0]

    # 3. Отмена
    cb_cancel = MagicMock(spec=CallbackQuery)
    cb_cancel.from_user = MagicMock(id=starosta_id)
    cb_cancel.message = MagicMock(spec=Message)
    cb_cancel.message.edit_text = AsyncMock()
    cb_cancel.answer = AsyncMock()
    mock_state = MagicMock(spec=FSMContext)
    mock_state.clear = AsyncMock()

    await cb_homework_cancel(cb_cancel, mock_state, database=test_db)
    assert "отменено" in cb_cancel.message.edit_text.call_args[0][0]

    # 4. Сложение полномочий старосты: запрос админу и подтверждение
    mock_bot_resign = MagicMock(spec=Bot)
    mock_bot_resign.send_message = AsyncMock()

    cb_resign = MagicMock(spec=CallbackQuery)
    cb_resign.from_user = MagicMock(id=starosta_id, username="starosta_login", full_name="Алексей")
    cb_resign.message = MagicMock(spec=Message)
    cb_resign.message.edit_text = AsyncMock()
    cb_resign.answer = AsyncMock()
    cb_resign.bot = mock_bot_resign

    await cb_resign_starosta(cb_resign, database=test_db)
    # Снять старосту может только админ -> староста еще не снят
    assert await test_db.get_group_starosta(group_id) is not None
    assert "Запрос отправлен" in cb_resign.message.edit_text.call_args[0][0]
    assert mock_bot_resign.send_message.called

    # Админ подтверждает снятие полномочий
    from handlers import cb_admin_starosta_decision
    from keyboards import AdminStarostaApproveCallback
    from config import config

    cb_admin_rem = MagicMock(spec=CallbackQuery)
    cb_admin_rem.from_user = MagicMock(id=config.ADMIN_IDS[0], full_name="Админ")
    cb_admin_rem.message = MagicMock(spec=Message)
    cb_admin_rem.message.edit_text = AsyncMock()
    cb_admin_rem.answer = AsyncMock()
    cb_admin_rem.bot = mock_bot_resign

    rem_data = AdminStarostaApproveCallback(
        action="rem_confirm", req_id=0, group_id=group_id, candidate_id=starosta_id
    )
    await cb_admin_starosta_decision(cb_admin_rem, rem_data, database=test_db)
    assert await test_db.get_group_starosta(group_id) is None


@pytest.mark.asyncio
async def test_starosta_rejection_flow(test_db):
    """Тест: администратор отклоняет заявку студента на пост старосты."""
    from handlers import cb_claim_starosta, cb_admin_starosta_decision
    from keyboards import AdminStarostaApproveCallback
    from config import config

    group_id = "grp_rej_test"
    student_id = 998877
    await test_db.upsert_groups_bulk([
        {"id": group_id, "specialty_name": "Экономика", "group_name": "ЭК-1", "relative_url": "/ek1"}
    ])
    await test_db.upsert_user(student_id, group_id, "/ek1", "ЭК-1", notifications_enabled=True)

    mock_bot = MagicMock(spec=Bot)
    mock_bot.send_message = AsyncMock()

    # Студент подает заявку
    cb_claim = MagicMock(spec=CallbackQuery)
    cb_claim.from_user = MagicMock(id=student_id, username="ek_stud", full_name="Екатерина")
    cb_claim.message = MagicMock(spec=Message)
    cb_claim.message.edit_text = AsyncMock()
    cb_claim.answer = AsyncMock()
    cb_claim.bot = mock_bot

    await cb_claim_starosta(cb_claim, database=test_db)
    req = await test_db.get_pending_starosta_request(group_id, student_id, "claim")
    assert req is not None
    req_id = req["id"]

    # Администратор отклоняет заявку
    cb_admin = MagicMock(spec=CallbackQuery)
    cb_admin.from_user = MagicMock(id=config.ADMIN_IDS[0], full_name="Админ")
    cb_admin.message = MagicMock(spec=Message)
    cb_admin.message.edit_text = AsyncMock()
    cb_admin.answer = AsyncMock()
    cb_admin.bot = mock_bot

    rej_data = AdminStarostaApproveCallback(
        action="reject", req_id=req_id, group_id=group_id, candidate_id=student_id
    )
    await cb_admin_starosta_decision(cb_admin, rej_data, database=test_db)

    # Проверяем, что староста НЕ назначен, а статус заявки rejected
    assert await test_db.get_group_starosta(group_id) is None
    updated_req = await test_db.get_starosta_request_by_id(req_id)
    assert updated_req["status"] == "rejected"
    assert "ОТКЛОНЕНА" in cb_admin.message.edit_text.call_args[0][0]


@pytest.mark.asyncio
async def test_non_starosta_cannot_add_hw(test_db):
    """Тест: обычный студент без подтвержденных прав старосты не может добавить ДЗ."""
    from handlers import cb_homework_add_start, process_hw_task_text
    from keyboards import HomeworkCallback

    group_id = "grp_perm_test"
    student_id = 121212
    await test_db.upsert_groups_bulk([
        {"id": group_id, "specialty_name": "ИТ", "group_name": "ИТ-99", "relative_url": "/it99"}
    ])
    await test_db.upsert_user(student_id, group_id, "/it99", "ИТ-99", notifications_enabled=True)

    # 1. Попытка нажать «Добавить ДЗ»
    cb_add = MagicMock(spec=CallbackQuery)
    cb_add.from_user = MagicMock(id=student_id)
    cb_add.answer = AsyncMock()
    mock_state = MagicMock(spec=FSMContext)

    await cb_homework_add_start(cb_add, mock_state, database=test_db)
    cb_add.answer.assert_called_once()
    assert "Только староста" in cb_add.answer.call_args[0][0]

    # 2. Попытка отправки сообщения с заданием в FSM без прав
    msg_task = MagicMock(spec=Message)
    msg_task.from_user = MagicMock(id=student_id)
    msg_task.text = "Фейковое ДЗ"
    msg_task.answer = AsyncMock()
    mock_state.get_data = AsyncMock(return_value={"group_id": group_id, "subject": "Тест", "lang": "ru"})
    mock_state.clear = AsyncMock()

    await process_hw_task_text(msg_task, mock_state, database=test_db)
    assert "Только подтвержденный староста" in msg_task.answer.call_args[0][0]
    assert len(await test_db.get_upcoming_homework(group_id)) == 0


@pytest.mark.asyncio
async def test_admin_commands_set_and_remove_starosta(test_db):
    """Тест команд администратора /set_starosta и /remove_starosta."""
    from handlers import cmd_admin_set_starosta, cmd_admin_remove_starosta
    from config import config

    group_id = "grp_adm_cmd"
    student_id = 777111
    admin_id = config.ADMIN_IDS[0]

    await test_db.upsert_groups_bulk([
        {"id": group_id, "specialty_name": "Связь", "group_name": "СС-21", "relative_url": "/ss21"}
    ])
    await test_db.upsert_user(student_id, group_id, "/ss21", "СС-21", username="trusted_stud", first_name="Павел")

    mock_bot = MagicMock(spec=Bot)
    mock_bot.send_message = AsyncMock()

    # 1. Обычный пользователь не может использовать команду
    msg_fail = MagicMock(spec=Message)
    msg_fail.from_user = MagicMock(id=student_id)
    msg_fail.text = f"/set_starosta {group_id} {student_id}"
    msg_fail.answer = AsyncMock()

    await cmd_admin_set_starosta(msg_fail, database=test_db)
    assert "Доступно только" in msg_fail.answer.call_args[0][0]

    # 2. Администратор назначает старосту напрямую
    msg_ok = MagicMock(spec=Message)
    msg_ok.from_user = MagicMock(id=admin_id)
    msg_ok.text = f"/set_starosta СС-21 @trusted_stud"
    msg_ok.bot = mock_bot
    msg_ok.answer = AsyncMock()

    await cmd_admin_set_starosta(msg_ok, database=test_db)
    assert "успешно назначен" in msg_ok.answer.call_args[0][0]

    starosta = await test_db.get_group_starosta(group_id)
    assert starosta is not None
    assert starosta["user_id"] == student_id

    # 3. Администратор снимает старосту
    msg_rem = MagicMock(spec=Message)
    msg_rem.from_user = MagicMock(id=admin_id)
    msg_rem.text = f"/remove_starosta СС-21"
    msg_rem.bot = mock_bot
    msg_rem.answer = AsyncMock()

    await cmd_admin_remove_starosta(msg_rem, database=test_db)
    assert "успешно снят" in msg_rem.answer.call_args[0][0]
    assert await test_db.get_group_starosta(group_id) is None


@pytest.mark.asyncio
async def test_group_menu_navigation(test_db):
    """Тест переключения между главным меню и меню группы."""
    from handlers import cmd_group_menu, cmd_back_to_main_menu

    user_id = 654321
    group_id = "grp_nav_test"
    await test_db.upsert_groups_bulk([
        {"id": group_id, "specialty_name": "ИТ", "group_name": "ИТ-31", "relative_url": "/it31"}
    ])
    await test_db.upsert_user(user_id, group_id, "/it31", "ИТ-31", notifications_enabled=True)

    # 1. Открытие меню группы
    msg_gm = MagicMock(spec=Message)
    msg_gm.chat = MagicMock(type="private", id=user_id)
    msg_gm.from_user = MagicMock(id=user_id)
    msg_gm.answer = AsyncMock()

    await cmd_group_menu(msg_gm, database=test_db)
    assert "Меню группы" in msg_gm.answer.call_args[0][0]
    kb_gm = msg_gm.answer.call_args[1]["reply_markup"]
    gm_buttons = [btn.text for row in kb_gm.keyboard for btn in row]
    assert any("ДЗ" in b for b in gm_buttons)
    assert any("Староста" in b for b in gm_buttons)
    assert any("Сменить группу" in b for b in gm_buttons)
    assert any("Главное меню" in b for b in gm_buttons)

    # 2. Возврат в главное меню
    msg_back = MagicMock(spec=Message)
    msg_back.from_user = MagicMock(id=user_id)
    msg_back.answer = AsyncMock()

    await cmd_back_to_main_menu(msg_back, database=test_db)
    assert "Главное меню" in msg_back.answer.call_args[0][0]
    kb_main = msg_back.answer.call_args[1]["reply_markup"]
    main_buttons = [btn.text for row in kb_main.keyboard for btn in row]
    assert any("На сегодня" in b for b in main_buttons)
    assert any("Меню группы" in b for b in main_buttons)


@pytest.mark.asyncio
async def test_bells_timetable_and_status(test_db):
    """Тест карточки звонков и динамического определения текущего статуса."""
    from keyboards import BELLS_TIMETABLE, get_bells_inline_keyboard
    from handlers import get_bells_card_text, cmd_bells

    assert len(BELLS_TIMETABLE) == 7
    assert BELLS_TIMETABLE[0]["start"] == "08:30"
    assert BELLS_TIMETABLE[1]["break_min"] == 10
    assert BELLS_TIMETABLE[2]["break_min"] == 40
    assert BELLS_TIMETABLE[2].get("is_big_break") is True

    from handlers import format_remaining_time
    assert format_remaining_time(25, "ru") == "25 мин"
    assert format_remaining_time(454, "ru") == "7 ч 34 мин"
    assert format_remaining_time(120, "ru") == "2 ч"
    assert format_remaining_time(25, "en") == "25 min"
    assert format_remaining_time(454, "en") == "7 h 34 min"

    # Проверка формирования текста для RU и EN
    text_ru = get_bells_card_text("ru")
    assert "Расписание звонков" in text_ru
    assert "08:30 – 10:00" in text_ru
    assert "большая перемена 40 мин" in text_ru

    text_en = get_bells_card_text("en")
    assert "Bell Timetable" in text_en
    assert "08:30 – 10:00" in text_en

    # Проверка кнопки обновить
    kb = get_bells_inline_keyboard("ru")
    assert any("Обновить статус" in btn.text for row in kb.inline_keyboard for btn in row)

    # Проверка хендлера cmd_bells
    msg = MagicMock(spec=Message)
    msg.from_user = MagicMock(id=999)
    msg.answer = AsyncMock()
    await cmd_bells(msg, database=test_db)
    assert "Расписание звонков" in msg.answer.call_args[0][0]


@pytest.mark.asyncio
async def test_group_chat_link_flow(test_db):
    """Тест сохранения, получения и удаления ссылки на беседу группы."""
    from keyboards import get_group_chat_link_keyboard

    group_id = "grp_chat_test"
    user_id = 112233
    await test_db.upsert_groups_bulk([
        {"id": group_id, "specialty_name": "ИТ", "group_name": "ИТ-11", "relative_url": "/it11"}
    ])
    await test_db.upsert_user(user_id, group_id, "/it11", "ИТ-11", notifications_enabled=True)

    # Изначально ссылки нет
    link_info = await test_db.get_group_chat_link(group_id)
    assert link_info is None

    # Добавляем ссылку
    await test_db.set_group_chat_link(group_id, "https://t.me/+AbCdEf123", user_id)
    link_info = await test_db.get_group_chat_link(group_id)
    assert link_info is not None
    assert link_info["chat_link"] == "https://t.me/+AbCdEf123"

    # Клавиатура со ссылкой
    kb = get_group_chat_link_keyboard("https://t.me/+AbCdEf123", is_starosta=True, lang="ru")
    assert any("Перейти в беседу" in btn.text for row in kb.inline_keyboard for btn in row)
    assert any("Изменить ссылку" in btn.text for row in kb.inline_keyboard for btn in row)
    assert any("Удалить ссылку" in btn.text for row in kb.inline_keyboard for btn in row)

    # Удаление ссылки
    deleted = await test_db.delete_group_chat_link(group_id)
    assert deleted is True
    assert await test_db.get_group_chat_link(group_id) is None


@pytest.mark.asyncio
async def test_campus_navigator_and_search(test_db):
    """Тест справочника корпусов и поиска аудиторий."""
    from handlers import CAMPUS_FLOORS, CAMPUS_ROOMS, cmd_campus, process_campus_search
    from keyboards import get_campus_inline_keyboard

    assert 1 in CAMPUS_FLOORS and 4 in CAMPUS_FLOORS
    assert "Столовая" in CAMPUS_FLOORS[1]["description"]
    assert "Деканат" in CAMPUS_FLOORS[2]["description"]
    assert "Библиотека" in CAMPUS_FLOORS[3]["description"]
    assert "Лаборатория физики" in CAMPUS_FLOORS[4]["description"]

    # Проверка клавиатуры этажей
    kb = get_campus_inline_keyboard(current_floor=2, lang="ru")
    floor_btns = [btn.text for row in kb.inline_keyboard for btn in row]
    assert any("2 этаж" in b for b in floor_btns)
    assert any("Найти аудиторию" in b for b in floor_btns)

    # Тест поиска по номеру кабинета 304
    msg = MagicMock(spec=Message)
    msg.from_user = MagicMock(id=555)
    msg.text = "304"
    msg.answer = AsyncMock()
    state = AsyncMock()
    await process_campus_search(msg, state, database=test_db)
    assert "Компьютерные классы" in msg.answer.call_args[0][0] or "304" in msg.answer.call_args[0][0]

    # Тест поиска ключевого слова "спортзал"
    msg.text = "спортзал"
    await process_campus_search(msg, state, database=test_db)
    assert "Спортивный зал" in msg.answer.call_args[0][0]


@pytest.mark.asyncio
async def test_my_teachers_extraction(test_db):
    """Тест списка преподавателей группы и их расписания."""
    from handlers import cmd_my_teachers

    group_id = "grp_teachers_test"
    user_id = 998877
    await test_db.upsert_groups_bulk([
        {"id": group_id, "specialty_name": "ИТ", "group_name": "ИТ-22", "relative_url": "/it22"}
    ])
    await test_db.upsert_user(user_id, group_id, "/it22", "ИТ-22", notifications_enabled=True)

    # Мокаем fetch_week_schedule
    mock_week = [
        {
            "date": "2026-09-15",
            "day_name": "Вторник",
            "lessons": [
                {"subject": "Математика", "teacher": "Петров П.П.", "room": "205"},
                {"subject": "Информатика", "teacher": "Сидоров С.С.", "room": "304"},
            ]
        }
    ]

    with patch("handlers.timetable_parser.fetch_week_schedule", AsyncMock(return_value=mock_week)):
        msg = MagicMock(spec=Message)
        msg.chat = MagicMock(type="private", id=user_id)
        msg.from_user = MagicMock(id=user_id)
        wait_msg = AsyncMock()
        msg.answer = AsyncMock(return_value=wait_msg)

        await cmd_my_teachers(msg, database=test_db)
        edited_text = wait_msg.edit_text.call_args[0][0]
        assert "Петров П.П." in edited_text
        assert "Сидоров С.С." in edited_text


@pytest.mark.asyncio
async def test_exams_session_crud(test_db):
    """Тест добавления, просмотра и удаления экзаменов/сессии."""
    group_id = "grp_exam_test"
    user_id = 445566
    await test_db.upsert_groups_bulk([
        {"id": group_id, "specialty_name": "ИТ", "group_name": "ИТ-23", "relative_url": "/it23"}
    ])
    await test_db.upsert_user(user_id, group_id, "/it23", "ИТ-23", notifications_enabled=True)

    # 1. Добавление экзаменов
    ex1_id = await test_db.add_group_exam(
        group_id=group_id,
        subject="Базы данных",
        exam_date="20.12.2026",
        exam_time="10:00",
        room="304",
        teacher="Кузнецов К.К.",
        author_id=user_id,
    )
    ex2_id = await test_db.add_group_exam(
        group_id=group_id,
        subject="Философия",
        exam_date="15.12.2026",
        exam_time="09:00",
        room="201",
        teacher="Смирнов С.С.",
        author_id=user_id,
    )

    # 2. Получение экзаменов (сортировка по дате)
    exams = await test_db.get_group_exams(group_id)
    assert len(exams) == 2
    assert exams[0]["subject"] == "Философия"  # 15.12 раньше 20.12

    # 3. Удаление одного экзамена
    deleted = await test_db.delete_group_exam(ex1_id, group_id)
    assert deleted is True
    exams_after = await test_db.get_group_exams(group_id)
    assert len(exams_after) == 1
    assert exams_after[0]["id"] == ex2_id


@pytest.mark.asyncio
async def test_user_notes_crud(test_db):
    """Тест персонального блокнота студента."""
    user1_id = 778899
    user2_id = 112299

    # Пользователь 1 добавляет заметку
    n1_id = await test_db.add_user_note(user1_id, "Сдать лабораторную по сетям")
    n2_id = await test_db.add_user_note(user1_id, "Купить тетрадь 48 листов")

    # Пользователь 2 добавляет свою заметку
    await test_db.add_user_note(user2_id, "Заметка другого студента")

    # Изоляция заметок
    u1_notes = await test_db.get_user_notes(user1_id)
    assert len(u1_notes) == 2
    assert u1_notes[0]["note_text"] == "Купить тетрадь 48 листов"  # DESC

    u2_notes = await test_db.get_user_notes(user2_id)
    assert len(u2_notes) == 1
    assert u2_notes[0]["note_text"] == "Заметка другого студента"

    # Удаление заметки
    deleted = await test_db.delete_user_note(n1_id, user1_id)
    assert deleted is True
    u1_remaining = await test_db.get_user_notes(user1_id)
    assert len(u1_remaining) == 1
    assert u1_remaining[0]["id"] == n2_id


@pytest.mark.asyncio
async def test_customizable_notification_time_slots(test_db):
    """Тест регулировки времени отправки вечерней и утренней рассылки."""
    u1 = 1001
    u2 = 1002
    u3 = 1003
    grp = "grp_time_test"
    await test_db.upsert_groups_bulk([
        {"id": grp, "specialty_name": "ИТ", "group_name": "ИТ-01", "relative_url": "/it01"}
    ])

    await test_db.upsert_user(u1, grp, "/it01", "ИТ-01", notifications_enabled=True)
    await test_db.upsert_user(u2, grp, "/it01", "ИТ-01", notifications_enabled=True)
    await test_db.upsert_user(u3, grp, "/it01", "ИТ-01", notifications_enabled=True)

    # u1 оставляет дефолт (20:00 вечер, 08:00 утро)
    # u2 выбирает 19:00 вечер, 07:30 утро
    await test_db.set_user_evening_time(u2, "19:00")
    await test_db.set_user_morning_time(u2, "07:30")

    # u3 выбирает 21:00 вечер, "off" утро
    await test_db.set_user_evening_time(u3, "21:00")
    await test_db.set_user_morning_time(u3, "off")

    # Проверяем подписчиков на вечерний слот 19:00
    subs_19 = await test_db.get_subscribers_for_evening_slot("19:00")
    assert grp in subs_19
    assert (u2, None) in subs_19[grp]
    assert (u1, None) not in subs_19[grp]

    # Проверяем подписчиков на дефолтный слот 20:00
    subs_20 = await test_db.get_subscribers_for_evening_slot("20:00")
    assert grp in subs_20
    assert (u1, None) in subs_20[grp]
    assert (u2, None) not in subs_20[grp]

    # Проверяем подписчиков на утренний слот 07:30
    subs_m_0730 = await test_db.get_subscribers_for_morning_slot("07:30")
    assert grp in subs_m_0730
    assert (u2, None) in subs_m_0730[grp]

    # u3 выключил утреннюю рассылку ("off"), поэтому он не попадает в утренние слоты
    subs_m_0800 = await test_db.get_subscribers_for_morning_slot("08:00")
    assert (u3, None) not in subs_m_0800.get(grp, [])


@pytest.mark.asyncio
async def test_notification_settings_interaction(test_db):
    """Тест интерактивного меню настройки уведомлений и коллбэков."""
    from handlers import handle_toggle_notifications, cb_notification_settings
    from keyboards import NotificationSettingCallback

    user_id = 9001
    grp = "grp_notif_ui"
    await test_db.upsert_groups_bulk([
        {"id": grp, "specialty_name": "ИТ", "group_name": "ИТ-99", "relative_url": "/it99"}
    ])
    await test_db.upsert_user(user_id, grp, "/it99", "ИТ-99", notifications_enabled=True)

    # 1. Открытие настроек уведомлений
    msg = MagicMock(spec=Message)
    msg.chat = MagicMock(type="private", id=user_id)
    msg.from_user = MagicMock(id=user_id)
    msg.answer = AsyncMock()

    await handle_toggle_notifications(msg, database=test_db)
    assert "Настройки времени рассылок" in msg.answer.call_args[0][0]

    # 2. Переключение времени вечерней рассылки через коллбэк
    cb_msg = AsyncMock(spec=Message)
    cb_msg.chat = MagicMock(type="private", id=user_id)
    cb = MagicMock(spec=CallbackQuery)
    cb.from_user = MagicMock(id=user_id)
    cb.message = cb_msg
    cb.answer = AsyncMock()

    cb_data = NotificationSettingCallback(target="evening_set", value="18:00")
    await cb_notification_settings(cb, cb_data, database=test_db)

    user = await test_db.get_user(user_id)
    assert user["evening_notify_time"] == "18:00"

    # 3. Переключение утреннего времени
    cb_data_m = NotificationSettingCallback(target="morning_set", value="07:00")
    await cb_notification_settings(cb, cb_data_m, database=test_db)

    user = await test_db.get_user(user_id)
    assert user["morning_notify_time"] == "07:00"


@pytest.mark.asyncio
async def test_starosta_deputy_system(test_db):
    """Тест назначения, получения и снятия заместителя старосты."""
    grp_id = "grp_test_deputy"
    starosta_id = 7701
    deputy_id = 7702

    await test_db.upsert_user(starosta_id, grp_id, "/url", "ИТ-11", username="starosta_user")
    await test_db.upsert_user(deputy_id, grp_id, "/url", "ИТ-11", username="deputy_user", first_name="Иван Заместитель")
    await test_db.set_group_starosta(grp_id, starosta_id, "starosta_user")

    # Изначально зама нет
    assert await test_db.get_group_deputy(grp_id) is None
    assert await test_db.is_starosta_or_deputy(grp_id, deputy_id) is False

    # Назначаем зама
    ok = await test_db.set_group_deputy(grp_id, deputy_id, "deputy_user", "Иван Заместитель")
    assert ok is True

    deputy = await test_db.get_group_deputy(grp_id)
    assert deputy is not None
    assert deputy["user_id"] == deputy_id
    assert deputy["username"] == "deputy_user"
    assert deputy["full_name"] == "Иван Заместитель"

    # Теперь зам имеет права
    assert await test_db.is_starosta_or_deputy(grp_id, deputy_id) is True
    assert await test_db.is_starosta_or_deputy(grp_id, starosta_id) is True
    assert await test_db.is_starosta_or_deputy(grp_id, 99999) is False

    # Проверка клавиатуры для старосты с замом
    from keyboards import get_starosta_inline_keyboard
    kb_starosta = get_starosta_inline_keyboard(is_current_user=True, has_starosta=True, has_deputy=True, is_deputy=False)
    btn_texts = [b.text for row in kb_starosta.inline_keyboard for b in row]
    assert any("Снять заместителя" in t for t in btn_texts)
    assert any("Добавить ДЗ" in t for t in btn_texts)

    # Проверка клавиатуры для самого зама
    kb_deputy = get_starosta_inline_keyboard(is_current_user=False, has_starosta=True, has_deputy=True, is_deputy=True)
    deputy_btns = [b.text for row in kb_deputy.inline_keyboard for b in row]
    assert any("Добавить ДЗ" in t for t in deputy_btns)
    assert not any("Снять заместителя" in t for t in deputy_btns)

    # Снятие зама
    removed = await test_db.remove_group_deputy(grp_id)
    assert removed is True
    assert await test_db.get_group_deputy(grp_id) is None
    assert await test_db.is_starosta_or_deputy(grp_id, deputy_id) is False


@pytest.mark.asyncio
async def test_notes_with_date_and_pair(test_db):
    """Тест создания заметок с датой, парой и проверка выборки напоминаний."""
    user_id = 8801
    await test_db.upsert_user(user_id, "g_notes", "/g_notes", "ИТ-22")

    note_id = await test_db.add_user_note(
        user_id=user_id,
        note_text="Сдать отчет по практике",
        target_date="2026-09-12",
        target_pair=2,
    )
    assert note_id > 0

    notes = await test_db.get_user_notes(user_id)
    assert len(notes) == 1
    assert notes[0]["note_text"] == "Сдать отчет по практике"
    assert notes[0]["target_date"] == "2026-09-12"
    assert notes[0]["target_pair"] == 2
    assert notes[0]["reminded"] == 0

    # Проверка выборки напоминаний на сегодня
    pending = await test_db.get_pending_note_reminders("2026-09-12")
    assert len(pending) == 1
    assert pending[0]["id"] == note_id

    # На дату до дедлайна напоминаний еще нет
    assert len(await test_db.get_pending_note_reminders("2026-09-11")) == 0

    # Отмечаем как напомненное
    await test_db.mark_note_reminded(note_id)
    assert len(await test_db.get_pending_note_reminders("2026-09-12")) == 0


@pytest.mark.asyncio
async def test_settings_info_menu_handler(test_db):
    """Тест вызова меню '⚙️ Настройки и связь'."""
    from handlers import cmd_settings_info_menu
    msg = MagicMock(spec=Message)
    msg.from_user = MagicMock(id=555)
    msg.answer = AsyncMock()

    await test_db.upsert_user(555, "grp_1", "/grp1", "1-ИС-1")
    await cmd_settings_info_menu(msg, database=test_db)

    assert msg.answer.called
    reply_markup = msg.answer.call_args[1]["reply_markup"]
    btn_texts = [b.text for row in reply_markup.keyboard for b in row]
    assert any("Язык" in t for t in btn_texts)
    assert any("Бот в группу" in t for t in btn_texts)
    assert any("Связь с автором" in t for t in btn_texts)
    assert any("Инструкция" in t for t in btn_texts)
    assert any("Главное меню" in t for t in btn_texts)


@pytest.mark.asyncio
async def test_menu_nav_reset_from_fsm_state(test_db):
    """Тест сброса состояния FSM при переходе в '⚙️ Настройки и связь' и в главное меню."""
    from handlers import cmd_settings_info_menu, cmd_back_to_main_menu
    from throttling import MenuNavResetMiddleware, is_nav_button_or_command

    # 1. Проверка распознавания навигационных кнопок и команд
    assert is_nav_button_or_command("⚙️ Настройки и связь") is True
    assert is_nav_button_or_command("⬅️ Главное меню") is True
    assert is_nav_button_or_command("/start") is True
    assert is_nav_button_or_command("304") is False
    assert is_nav_button_or_command("купить тетрадь") is False

    # 2. Проверка сброса состояния в cmd_settings_info_menu
    msg = MagicMock(spec=Message)
    msg.from_user = MagicMock(id=555)
    msg.answer = AsyncMock()
    mock_state = AsyncMock(spec=FSMContext)

    await cmd_settings_info_menu(msg, state=mock_state, database=test_db)
    mock_state.clear.assert_awaited_once()

    # 3. Проверка сброса состояния в cmd_back_to_main_menu
    mock_state.clear.reset_mock()
    await cmd_back_to_main_menu(msg, state=mock_state, database=test_db)
    mock_state.clear.assert_awaited_once()

    # 4. Проверка работы MenuNavResetMiddleware
    middleware = MenuNavResetMiddleware()
    event = MagicMock(spec=Message)
    event.text = "⚙️ Настройки и связь"
    mock_fsm = AsyncMock()
    mock_fsm.get_state = AsyncMock(return_value="SomeState:waiting")
    mock_fsm.clear = AsyncMock()
    data = {"state": mock_fsm}

    handler_called = False
    async def dummy_handler(evt, d):
        nonlocal handler_called
        handler_called = True
        return "OK"

    result = await middleware(dummy_handler, event, data)
    assert result == "OK"
    assert handler_called is True
    mock_fsm.clear.assert_awaited_once()


@pytest.mark.asyncio
async def test_schedule_image_cache_in_database(test_db):
    """Тест сохранения Telegram file_id картинки расписания в SQLite и инвалидации при изменении."""
    date_str = "2026-09-12"
    group_id = "grp_test_img"

    # 1. До сохранения кэша — None
    assert await test_db.get_cached_schedule_image(group_id, date_str) is None

    # 2. Сохраняем file_id
    test_file_id = "AgACAgIAAxkBAAI..."
    await test_db.save_cached_schedule_image(group_id, date_str, test_file_id)

    # 3. Проверяем получение сохраненного file_id
    cached = await test_db.get_cached_schedule_image(group_id, date_str)
    assert cached == test_file_id

    # 4. Проверяем сохранение расписания с тем же хэшем (file_id должен остаться)
    sched_v1 = {"lessons": [{"subject": "Математика"}], "date": date_str}
    await test_db.save_cached_timetable(group_id, date_str, sched_v1, data_hash="hash_1")
    # Пересохраним картинку для hash_1
    await test_db.save_cached_schedule_image(group_id, date_str, "file_id_v1")
    assert await test_db.get_cached_schedule_image(group_id, date_str) == "file_id_v1"

    # 5. При изменении хэша (замена/новое расписание) image_file_id сбрасывается для перерисовки
    sched_v2 = {"lessons": [{"subject": "Физика"}], "date": date_str}
    await test_db.save_cached_timetable(group_id, date_str, sched_v2, data_hash="hash_2")
    assert await test_db.get_cached_schedule_image(group_id, date_str) is None


@pytest.mark.asyncio
async def test_send_day_schedule_with_image_and_caching(test_db):
    """Тест автоматической выдачи картинки и запоминания file_id при запросе расписания."""
    from handlers import send_day_schedule_with_image, _ram_image_cache, clear_ram_image_cache
    clear_ram_image_cache()

    target_date = date(2026, 9, 12)
    date_str = target_date.isoformat()
    group_id = "grp_cached_1"
    group_name = "1-ИС-10"

    sched_data = {
        "date": date_str,
        "day_name": "Суббота",
        "week_number": 2,
        "is_even_week": True,
        "lessons": [
            {
                "pair_number": 1,
                "time": "08:30 - 10:00",
                "subject": "Информатика",
                "room": "101",
                "teacher": "Иванов И.И.",
                "subgroup": 0,
            }
        ],
    }

    mock_bot = MagicMock(spec=Bot)
    mock_bot.get_me = AsyncMock(return_value=MagicMock(username="ktmu_bot"))

    sent_message = MagicMock(spec=Message)
    sent_photo_obj = MagicMock(file_id="telegram_file_id_12345")
    sent_message.photo = [sent_photo_obj]
    mock_bot.send_photo = AsyncMock(return_value=sent_message)

    with patch("handlers.render_schedule_image") as mock_render:
        import io
        from PIL import Image
        img = Image.new("RGB", (100, 100), color="white")
        buf = io.BytesIO()
        img.save(buf, format="PNG")
        buf.seek(0)
        mock_render.return_value = buf

        # 1. Первый запрос: картинка рендерится через Pillow, отправляется photo и запоминается
        msg1 = await send_day_schedule_with_image(
            bot=mock_bot,
            chat_id=123,
            database=test_db,
            group_id=group_id,
            group_name=group_name,
            target_date=target_date,
            sched=sched_data,
        )
        assert msg1 is not None
        assert mock_render.call_count == 1
        assert mock_bot.send_photo.call_count == 1
        # Проверяем, что в БД и RAM запомнился file_id
        assert _ram_image_cache.get((group_id, date_str)) == "telegram_file_id_12345"
        assert await test_db.get_cached_schedule_image(group_id, date_str) == "telegram_file_id_12345"

        # 2. Второй запрос от другого пользователя/группы для этого же расписания
        mock_render.reset_mock()
        mock_bot.send_photo.reset_mock()
        mock_bot.send_photo.return_value = sent_message

        msg2 = await send_day_schedule_with_image(
            bot=mock_bot,
            chat_id=456,  # Другой чат/студент!
            database=test_db,
            group_id=group_id,
            group_name=group_name,
            target_date=target_date,
            sched=sched_data,
        )
        assert msg2 is not None
        # Проверяем: render_schedule_image НЕ вызывался повторно!
        assert mock_render.call_count == 0
        # send_photo вызвался напрямую со строковым file_id!
        assert mock_bot.send_photo.call_count == 1
        call_kwargs = mock_bot.send_photo.call_args[1]
        assert call_kwargs["photo"] == "telegram_file_id_12345"
        assert call_kwargs["chat_id"] == 456


@pytest.mark.asyncio
async def test_get_font_bundled():
    """Тест загрузки бандлованных шрифтов с поддержкой кириллицы."""
    from image_generator import _get_font, render_schedule_image

    font_regular = _get_font(18, bold=False)
    font_bold = _get_font(24, bold=True)
    assert font_regular is not None
    assert font_bold is not None

    sample_day = {
        "date": "2026-09-13",
        "day_name": "Понедельник",
        "week_number": 2,
        "is_even_week": True,
        "lessons": [
            {
                "pair_number": 1,
                "time": "08:30-10:00",
                "subject": "Математика и криптография",
                "room": "305",
                "teacher": "Смирнов А.А.",
            }
        ],
    }
    img_io = render_schedule_image(sample_day, "1-ИС-2", bot_username="schedulektmubot")
    assert img_io is not None
    assert len(img_io.getvalue()) > 10000


@pytest.mark.asyncio
async def test_admin_backup_and_restore_db(test_db, tmp_path):
    """Тест выгрузки бэкапа БД и восстановления базы администратором."""
    from handlers import cmd_backup_db, process_admin_restore_db
    from aiogram.types import User
    import sqlite3

    # 1. Тест cmd_backup_db
    user_admin = User(id=870396858, is_bot=False, first_name="Админ")
    msg_admin = MagicMock(spec=Message)
    msg_admin.from_user = user_admin
    msg_admin.answer_document = AsyncMock()

    await cmd_backup_db(msg_admin, database=test_db)
    assert msg_admin.answer_document.called

    # 2. Создаем тестовую валидную БД для импорта
    new_db_file = tmp_path / "valid_restore.db"
    import shutil
    shutil.copy2(test_db.db_path, new_db_file)
    conn = sqlite3.connect(new_db_file)
    try:
        conn.execute("INSERT OR REPLACE INTO users (user_id, first_name) VALUES (999, 'НовыйСтудент');")
        conn.commit()
    finally:
        conn.close()

    # 3. Тест process_admin_restore_db
    mock_bot = MagicMock()
    mock_file = MagicMock()
    mock_file.file_path = "path/on/server"
    mock_bot.get_file = AsyncMock(return_value=mock_file)

    async def fake_download(file_path, destination):
        import shutil
        shutil.copy2(new_db_file, destination)

    mock_bot.download_file = AsyncMock(side_effect=fake_download)

    mock_doc = MagicMock()
    mock_doc.file_name = "ktmu_bot.db"
    mock_doc.file_id = "doc123"

    msg_restore = MagicMock(spec=Message)
    msg_restore.from_user = user_admin
    msg_restore.document = mock_doc
    status_mock = MagicMock()
    status_mock.edit_text = AsyncMock()
    msg_restore.answer = AsyncMock(return_value=status_mock)

    mock_state = AsyncMock(spec=FSMContext)

    await process_admin_restore_db(msg_restore, mock_state, mock_bot, database=test_db)
    assert mock_state.clear.called
    assert "успешно загружена и восстановлена" in status_mock.edit_text.call_args[0][0]

    # Проверяем, что в test_db теперь есть новый студент
    user_check = await test_db.get_user(999)
    assert user_check is not None
