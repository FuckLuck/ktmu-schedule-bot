import logging
from datetime import datetime, time, timedelta
from zoneinfo import ZoneInfo

from aiogram import F, Router
from aiogram.filters import Command, CommandStart
from aiogram.types import CallbackQuery, Message

from config import config
from database import Database, db as default_db
from keyboards import (
    GroupCallback,
    NavigationCallback,
    ScheduleNavCallback,
    SpecialtyCallback,
    get_groups_inline_keyboard,
    get_main_reply_keyboard,
    get_schedule_nav_keyboard,
    get_specialties_inline_keyboard,
)
from timetable_parser import (
    format_day_schedule_message,
    format_week_schedule_messages,
    timetable_parser,
)

logger = logging.getLogger(__name__)

router = Router(name="main_router")


def get_current_college_time() -> datetime:
    """
    Возвращает текущую дату и время с учетом часового пояса колледжа.
    """
    try:
        tz = ZoneInfo(config.TIMEZONE)
        return datetime.now(tz)
    except Exception:
        return datetime.now()


def are_today_pairs_finished(lessons: list[dict], current_dt: datetime) -> bool:
    """
    Проверяет, завершились ли уже пары на текущий день.
    - Если пар нет (выходной/воскресенье) -> True.
    - Если время окончания последней пары уже прошло -> True.
    Например, если последняя пара закончилась в 15:30, а сейчас 16:00 -> вернет True.
    """
    if not lessons:
        return True

    latest_time = None
    for l in lessons:
        end_time_str = l.get("end_time", "").strip()
        if not end_time_str:
            continue
        try:
            parts = [int(p) for p in end_time_str.split(":")]
            t = time(hour=parts[0], minute=parts[1])
            if latest_time is None or t > latest_time:
                latest_time = t
        except Exception:
            continue

    if latest_time is None:
        latest_time = time(17, 10)

    return current_dt.time() >= latest_time


# -------------------------------------------------------------------------
# КОМАНДЫ СТАРТА И ВЫБОРА ГРУППЫ
# -------------------------------------------------------------------------

@router.message(CommandStart())
async def cmd_start(message: Message, database: Database = default_db):
    """
    Хэндлер команды /start.
    Если пользователь новый или не выбрал группу -> запускает выбор специальности.
    Если группа уже выбрана -> приветствует и показывает главное меню.
    """
    user_id = message.from_user.id
    user = await database.get_user(user_id)

    if not user or not user.get("group_id"):
        # Пользователь новый -> запускаем пошаговый выбор группы без кнопки «Вперед»
        specialties = await database.get_all_specialties()
        if not specialties:
            await message.answer(
                "⚠️ Список специальностей сейчас пуст. Пожалуйста, попробуйте через минуту — "
                "бот выполняет начальную синхронизацию с сайтом колледжа."
            )
            return

        kb = get_specialties_inline_keyboard(specialties, show_back_to_menu=False)
        await message.answer(
            "👋 <b>Добро пожаловать в бот расписания КТМУ!</b>\n\n"
            "Пожалуйста, выберите вашу <b>специальность</b> (Шаг 1 из 2):",
            reply_markup=kb,
            parse_mode="HTML"
        )
    else:
        group_name = user.get("group_name", "не указана")
        notif = user.get("notifications_enabled", True)
        kb = get_main_reply_keyboard(notifications_enabled=notif)
        await message.answer(
            f"👋 С возвращением! Ваша текущая группа: <b>{group_name}</b>.\n\n"
            "Воспользуйтесь кнопками меню ниже для просмотра расписания:",
            reply_markup=kb,
            parse_mode="HTML"
        )


@router.message(Command("change_group"))
@router.message(F.text == "⚙️ Сменить группу")
async def cmd_change_group(message: Message, database: Database = default_db):
    """
    Запуск диалога смены группы (нажатие кнопки «⚙️ Сменить группу» или команда /change_group).
    Показывает полный список специальностей без кнопки «Вперед» и с кнопкой «Назад в меню».
    """
    specialties = await database.get_all_specialties()
    if not specialties:
        await message.answer("⚠️ Не удалось загрузить специальности из базы данных.")
        return

    kb = get_specialties_inline_keyboard(specialties, show_back_to_menu=True)
    await message.answer(
        "🔄 <b>Смена учебной группы</b>\n\n"
        "Выберите вашу <b>специальность</b> (Шаг 1 из 2):",
        reply_markup=kb,
        parse_mode="HTML"
    )


# -------------------------------------------------------------------------
# ОБРАБОТЧИКИ INLINE-КНОПОК ВЫБОРА ГРУППЫ И НАВИГАЦИИ
# -------------------------------------------------------------------------

@router.callback_query(NavigationCallback.filter(F.to == "specialties"))
async def cb_back_to_specialties(callback: CallbackQuery, database: Database = default_db):
    """
    Кнопка «Назад к специальностям».
    """
    specialties = await database.get_all_specialties()
    kb = get_specialties_inline_keyboard(specialties, show_back_to_menu=True)
    await callback.message.edit_text(
        "Выберите вашу <b>специальность</b> (Шаг 1 из 2):",
        reply_markup=kb,
        parse_mode="HTML"
    )
    await callback.answer()


@router.callback_query(NavigationCallback.filter(F.to == "main_menu"))
async def cb_back_to_main_menu(callback: CallbackQuery, database: Database = default_db):
    """
    Кнопка «Назад в главное меню».
    """
    user = await database.get_user(callback.from_user.id)
    notif = user.get("notifications_enabled", True) if user else True
    kb = get_main_reply_keyboard(notifications_enabled=notif)

    try:
        await callback.message.delete()
    except Exception:
        pass

    await callback.message.answer(
        "🏠 <b>Главное меню</b>\n\nИспользуйте кнопки меню ниже для просмотра расписания:",
        reply_markup=kb,
        parse_mode="HTML"
    )
    await callback.answer()


@router.callback_query(SpecialtyCallback.filter(F.action == "pick"))
async def cb_select_specialty(
    callback: CallbackQuery, callback_data: SpecialtyCallback, database: Database = default_db
):
    """
    Шаг 2: Пользователь нажал на специальность -> показываем список групп.
    """
    specialties = await database.get_all_specialties()
    idx = callback_data.idx

    if idx < 0 or idx >= len(specialties):
        await callback.answer("Ошибка выбора специальности. Попробуйте снова.", show_alert=True)
        return

    selected_specialty = specialties[idx]
    groups = await database.get_groups_by_specialty(selected_specialty)

    if not groups:
        await callback.answer("Для этой специальности группы не найдены.", show_alert=True)
        return

    kb = get_groups_inline_keyboard(groups)
    await callback.message.edit_text(
        f"🎯 Специальность:\n<b>{selected_specialty}</b>\n\n"
        "Теперь выберите вашу <b>группу</b> (Шаг 2 из 2):",
        reply_markup=kb,
        parse_mode="HTML"
    )
    await callback.answer()


@router.callback_query(GroupCallback.filter(F.action == "pick"))
async def cb_select_group(
    callback: CallbackQuery, callback_data: GroupCallback, database: Database = default_db
):
    """
    Финал выбора: Пользователь выбрал группу -> сохраняем в users и открываем главное меню.
    """
    group_id = callback_data.group_id
    group = await database.get_group_by_id(group_id)

    if not group:
        await callback.answer("Информация о группе не найдена.", show_alert=True)
        return

    user_id = callback.from_user.id
    group_name = group["group_name"]
    relative_url = group["relative_url"]
    full_group_url = f"{config.BASE_URL.rstrip('/')}{relative_url}"

    # Сохраняем пользователя в SQLite
    await database.upsert_user(
        user_id=user_id,
        group_id=group_id,
        group_url=full_group_url,
        group_name=group_name,
        notifications_enabled=True,
    )

    # Удаляем inline-меню выбора
    try:
        await callback.message.delete()
    except Exception:
        pass

    # Отправляем подтверждение с главным меню
    kb = get_main_reply_keyboard(notifications_enabled=True)
    await callback.message.answer(
        f"✅ <b>Группа успешно выбрана:</b> <code>{group_name}</code>\n"
        f"🔗 Ссылка на расписание: <a href='{full_group_url}'>{relative_url}</a>\n\n"
        "🔔 Ежедневные уведомления в 20:00 (вечер) и в 08:00 (утро), а также напоминания о начале пар включены.\n"
        "Используйте кнопки меню ниже для просмотра расписания:",
        reply_markup=kb,
        parse_mode="HTML",
        disable_web_page_preview=True
    )
    await callback.answer("Группа успешно сохранена!")


# -------------------------------------------------------------------------
# ОСНОВНОЙ ФУНКЦИОНАЛ РАСПИСАНИЯ
# -------------------------------------------------------------------------

async def _get_authorized_user(message: Message, database: Database) -> dict | None:
    """Вспомогательная функция проверки привязки группы у пользователя."""
    user = await database.get_user(message.from_user.id)
    if not user or not user.get("group_id"):
        await message.answer(
            "⚠️ Сначала выберите вашу группу с помощью команды /start или /change_group."
        )
        return None
    return user


@router.message(F.text == "📅 На сегодня")
@router.message(Command("today"))
async def handle_schedule_today(message: Message, database: Database = default_db):
    """
    Расписание на сегодня.
    Если пары на сегодня уже закончились (например, сейчас 16:00, а последняя пара закончилась в 15:30) ->
    автоматически показывает расписание на завтра (на четверг) с кнопкой просмотра прошедшего дня!
    """
    user = await _get_authorized_user(message, database)
    if not user:
        return

    now_dt = get_current_college_time()
    today = now_dt.date()
    group_id = user["group_id"]
    group_name = user["group_name"]
    group_url = user["group_url"]

    wait_msg = await message.answer("⏳ <i>Загружаю расписание...</i>", parse_mode="HTML")

    try:
        today_sched = await timetable_parser.fetch_day_schedule(
            group_id=group_id, group_url=group_url, target_date=today
        )
        lessons = today_sched.get("lessons", [])

        # Проверяем, завершились ли уже пары на сегодня
        if are_today_pairs_finished(lessons, now_dt):
            tomorrow = today + timedelta(days=1)
            tomorrow_sched = await timetable_parser.fetch_day_schedule(
                group_id=group_id, group_url=group_url, target_date=tomorrow
            )
            schedule_text = format_day_schedule_message(tomorrow_sched, group_name)
            notice = (
                "🔔 <b>Пары на сегодня уже закончились!</b>\n"
                "📆 <i>Показываю расписание на завтра:</i>\n\n"
            )
            full_text = notice + schedule_text
            kb = get_schedule_nav_keyboard(
                current_date_str=today.isoformat(),
                show_today_past=bool(lessons)
            )
            await wait_msg.edit_text(full_text, reply_markup=kb, parse_mode="HTML")
        else:
            # Пары еще не закончились
            text = format_day_schedule_message(today_sched, group_name)
            kb = get_schedule_nav_keyboard(
                current_date_str=today.isoformat(),
                show_today_past=False
            )
            await wait_msg.edit_text(text, reply_markup=kb, parse_mode="HTML")

    except Exception as e:
        logger.error("Ошибка получения расписания на сегодня: %s", e, exc_info=True)
        await wait_msg.edit_text("❌ Не удалось загрузить расписание. Пожалуйста, попробуйте позже.")


@router.message(F.text == "📆 На завтра")
@router.message(Command("tomorrow"))
async def handle_schedule_tomorrow(message: Message, database: Database = default_db):
    """
    Расписание на завтра.
    """
    user = await _get_authorized_user(message, database)
    if not user:
        return

    tomorrow = get_current_college_time().date() + timedelta(days=1)
    group_id = user["group_id"]
    group_name = user["group_name"]
    group_url = user["group_url"]

    wait_msg = await message.answer("⏳ <i>Загружаю расписание на завтра...</i>", parse_mode="HTML")

    try:
        sched = await timetable_parser.fetch_day_schedule(
            group_id=group_id, group_url=group_url, target_date=tomorrow
        )
        text = format_day_schedule_message(sched, group_name)
        kb = get_schedule_nav_keyboard(
            current_date_str=tomorrow.isoformat(),
            show_today_past=False
        )
        await wait_msg.edit_text(text, reply_markup=kb, parse_mode="HTML")
    except Exception as e:
        logger.error("Ошибка получения расписания на завтра: %s", e, exc_info=True)
        await wait_msg.edit_text("❌ Не удалось загрузить расписание на завтра. Пожалуйста, попробуйте позже.")


@router.callback_query(ScheduleNavCallback.filter())
async def cb_schedule_nav(
    callback: CallbackQuery, callback_data: ScheduleNavCallback, database: Database = default_db
):
    """
    Обработчик инлайн-кнопок навигации по расписанию:
    - today_past: просмотр прошедшего за сегодня
    - tomorrow: переход к расписанию на завтра
    - week: расписание на неделю
    """
    user = await database.get_user(callback.from_user.id)
    if not user or not user.get("group_id"):
        await callback.answer("Сначала выберите группу: /start", show_alert=True)
        return

    group_id = user["group_id"]
    group_name = user["group_name"]
    group_url = user["group_url"]
    action = callback_data.action

    try:
        now_dt = get_current_college_time()
        today = now_dt.date()

        if action == "today_past":
            sched = await timetable_parser.fetch_day_schedule(
                group_id=group_id, group_url=group_url, target_date=today
            )
            text = "📅 <b>Прошедшее расписание за сегодня:</b>\n\n" + format_day_schedule_message(sched, group_name)
            kb = get_schedule_nav_keyboard(current_date_str=today.isoformat(), show_today_past=False)
            await callback.message.edit_text(text, reply_markup=kb, parse_mode="HTML")
            await callback.answer()

        elif action == "tomorrow":
            tomorrow = today + timedelta(days=1)
            sched = await timetable_parser.fetch_day_schedule(
                group_id=group_id, group_url=group_url, target_date=tomorrow
            )
            text = format_day_schedule_message(sched, group_name)
            kb = get_schedule_nav_keyboard(current_date_str=tomorrow.isoformat(), show_today_past=True)
            await callback.message.edit_text(text, reply_markup=kb, parse_mode="HTML")
            await callback.answer()

        elif action == "week":
            await callback.answer("Формирую расписание на неделю...")
            week_data = await timetable_parser.fetch_week_schedule(
                group_id=group_id, group_url=group_url, start_date=today
            )
            chunks = format_week_schedule_messages(week_data, group_name)
            for chunk in chunks:
                await callback.message.answer(chunk, parse_mode="HTML")

    except Exception as e:
        logger.error("Ошибка навигации по расписанию: %s", e, exc_info=True)
        await callback.answer("Не удалось обновить расписание.", show_alert=True)


@router.message(F.text == "🗓 На неделю")
@router.message(Command("week"))
async def handle_schedule_week(message: Message, database: Database = default_db):
    """
    Расписание на текущую неделю (понедельник — суббота).
    """
    user = await _get_authorized_user(message, database)
    if not user:
        return

    today = get_current_college_time().date()
    group_id = user["group_id"]
    group_name = user["group_name"]
    group_url = user["group_url"]

    wait_msg = await message.answer("⏳ <i>Формирую расписание на неделю...</i>", parse_mode="HTML")

    try:
        week_data = await timetable_parser.fetch_week_schedule(
            group_id=group_id, group_url=group_url, start_date=today
        )
        chunks = format_week_schedule_messages(week_data, group_name)

        await wait_msg.delete()
        for chunk in chunks:
            await message.answer(chunk, parse_mode="HTML")
    except Exception as e:
        logger.error("Ошибка получения расписания на неделю: %s", e, exc_info=True)
        await wait_msg.edit_text("❌ Не удалось загрузить расписание на неделю. Пожалуйста, попробуйте позже.")


# -------------------------------------------------------------------------
# ПЕРЕКЛЮЧЕНИЕ УВЕДОМЛЕНИЙ
# -------------------------------------------------------------------------

@router.message(F.text.startswith("🔔 Уведомления") | F.text.startswith("🔕 Уведомления"))
@router.message(Command("notifications"))
async def handle_toggle_notifications(message: Message, database: Database = default_db):
    """
    Включение/отключение автоматических уведомлений.
    """
    user_id = message.from_user.id
    user = await database.get_user(user_id)
    if not user:
        await message.answer("Сначала выберите группу: /start")
        return

    new_status = await database.toggle_notifications(user_id)
    kb = get_main_reply_keyboard(notifications_enabled=new_status)

    if new_status:
        text = (
            "🔔 <b>Уведомления включены!</b>\n\n"
            "Вы будете получать:\n"
            "• Расписание на завтра каждый вечер в 20:00\n"
            "• Точечные напоминания за 0 минут до начала каждой пары текущего дня."
        )
    else:
        text = (
            "🔕 <b>Уведомления отключены.</b>\n"
            "Вы не будете получать автоматические рассылки и напоминания о парах."
        )

    await message.answer(text, reply_markup=kb, parse_mode="HTML")


from aiogram.types import InlineKeyboardButton
from aiogram.utils.keyboard import InlineKeyboardBuilder


@router.message(Command("author"))
@router.message(Command("contact"))
@router.message(F.text == "👨‍💻 Связь с автором")
async def cmd_author(message: Message):
    """
    Связь с автором / разработчиком бота.
    """
    builder = InlineKeyboardBuilder()
    builder.row(
        InlineKeyboardButton(
            text="💬 Написать автору (@yapsychokid)",
            url="https://t.me/yapsychokid"
        )
    )
    author_text = (
        "👨‍💻 <b>Разработчик бота:</b> @yapsychokid\n\n"
        "По всем вопросам работы расписания, предложениям, найденным багам "
        "или сотрудничеству обращайтесь напрямую: @yapsychokid"
    )
    await message.answer(author_text, reply_markup=builder.as_markup(), parse_mode="HTML")


@router.message(Command("help"))
async def cmd_help(message: Message):
    """
    Справка по доступным командам.
    """
    builder = InlineKeyboardBuilder()
    builder.row(
        InlineKeyboardButton(
            text="💬 Связь с автором (@yapsychokid)",
            url="https://t.me/yapsychokid"
        )
    )
    help_text = (
        "📖 <b>Справка по боту расписания КТМУ</b>\n\n"
        "Доступные команды:\n"
        "/start — Запуск бота и выбор группы\n"
        "/today — Расписание на сегодня\n"
        "/tomorrow — Расписание на завтра\n"
        "/week — Расписание на текущую неделю\n"
        "/change_group — Сменить учебную группу\n"
        "/notifications — Вкл/выкл уведомления\n"
        "/author — Связь с разработчиком\n"
        "/help — Показать эту справку\n\n"
        "👨‍💻 <b>Автор / Разработчик:</b> @yapsychokid\n"
        "🌐 Источник данных: <a href='https://timetable-ktmu.ru/'>timetable-ktmu.ru</a>"
    )
    await message.answer(help_text, reply_markup=builder.as_markup(), parse_mode="HTML", disable_web_page_preview=True)
