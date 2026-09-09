import asyncio
import os
import pytest
import pytest_asyncio
from datetime import date, datetime, timedelta
from unittest.mock import AsyncMock, MagicMock, patch

from aiogram.exceptions import TelegramForbiddenError

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
    assert "Пара началась" in notif_msg
    assert "08:30 - 10:00" in notif_msg

    # 4. Форматирование недели
    week_msgs = format_week_schedule_messages([sample_day, empty_day], "1-ИС-10")
    assert len(week_msgs) >= 1
    assert "Расписание на неделю" in week_msgs[0]


from handlers import are_today_pairs_finished
from keyboards import (
    GroupCallback,
    SpecialtyCallback,
    get_groups_inline_keyboard,
    get_main_reply_keyboard,
    get_schedule_nav_keyboard,
    get_specialties_inline_keyboard,
)


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

    # 4. Главное меню
    reply_kb = get_main_reply_keyboard(notifications_enabled=True)
    assert len(reply_kb.keyboard) == 3
    assert any("На сегодня" in btn.text for row in reply_kb.keyboard for btn in row)
    assert any("ВКЛ" in btn.text for row in reply_kb.keyboard for btn in row)
    assert any("Связь с автором" in btn.text for row in reply_kb.keyboard for btn in row)


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

    # Будущее время старта пары сегодня (например, 23:59 чтобы точно было в будущем)
    future_time_str = "23:59"
    sample_sched = {
        "date": scheduler.get_current_date().isoformat(),
        "day_name": "День",
        "week_number": 1,
        "lessons": [
            {
                "pair_number": 1,
                "time": f"{future_time_str} - 01:00",
                "start_time": future_time_str,
                "subject": "Спецпредмет",
                "room": "100",
                "teacher": "Профессор",
                "format": "Очно",
                "subgroup": 0,
            }
        ],
    }

    with patch("scheduler.timetable_parser.fetch_day_schedule", new_callable=AsyncMock) as mock_fetch:
        mock_fetch.return_value = sample_sched

        await scheduler.schedule_today_pairs_notifications()

        # Проверяем, что в scheduler была добавлена точечная задача на пару
        jobs = scheduler.scheduler.get_jobs()
        pair_jobs = [j for j in jobs if j.id.startswith("pair_grp_beta")]
        assert len(pair_jobs) == 1
        assert pair_jobs[0].kwargs["group_id"] == "grp_beta"
        assert pair_jobs[0].kwargs["group_name"] == "Бета-1"


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

