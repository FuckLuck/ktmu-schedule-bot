import asyncio
import logging
from datetime import datetime, time, timedelta
from zoneinfo import ZoneInfo

from aiogram import Bot, F, Router
from aiogram.filters import Command, CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import CallbackQuery, ChatMemberUpdated, Message

from config import config
from database import Database, db as default_db
from keyboards import (
    AdminCallback,
    GroupCallback,
    HelpCallback,
    NavigationCallback,
    ScheduleNavCallback,
    SpecialtyCallback,
    get_admin_back_inline_keyboard,
    get_admin_main_inline_keyboard,
    get_broadcast_cancel_inline_keyboard,
    get_groups_inline_keyboard,
    get_help_inline_keyboard,
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


class AdminStates(StatesGroup):
    waiting_for_broadcast_text = State()


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


async def is_group_admin(bot: Bot, chat_id: int, user_id: int) -> bool:
    """
    Проверяет, является ли пользователь администратором или создателем чата.
    Для личных сообщений (chat_id > 0) всегда возвращает True.
    """
    if chat_id > 0:
        return True
    try:
        member = await bot.get_chat_member(chat_id=chat_id, user_id=user_id)
        return member.status in ("creator", "administrator")
    except Exception as e:
        logger.debug("Не удалось проверить статус пользователя %d в чате %d: %s", user_id, chat_id, e)
        return True


# -------------------------------------------------------------------------
# ОБРАБОТКА ДОБАВЛЕНИЯ БОТА В ГРУППУ (my_chat_member)
# -------------------------------------------------------------------------

@router.my_chat_member()
async def on_my_chat_member_updated(event: ChatMemberUpdated, database: Database = default_db):
    """
    Срабатывает при добавлении бота в группу или супергруппу.
    Приветствует участников и предлагает администраторам выбрать учебную группу.
    """
    if event.new_chat_member.status in ("member", "administrator"):
        chat = event.chat
        if chat.type in ("group", "supergroup"):
            specialties = await database.get_all_specialties()
            kb = get_specialties_inline_keyboard(specialties, show_back_to_menu=False)
            welcome_text = (
                "👋 <b>Всем привет! Я бот расписания колледжа КТМУ.</b>\n\n"
                "Я буду <b>каждое утро к 08:00</b> автоматически присылать в этот чат расписание занятий!\n\n"
                "Доступные команды в чате:\n"
                "• /today — Расписание на сегодня\n"
                "• /tomorrow — Расписание на завтра\n"
                "• /week — Расписание на неделю\n"
                "• /set_group — Настройка учебной группы для этого чата\n\n"
                "👇 <b>Администратор, выберите группу колледжа для этого чата:</b>"
            )
            try:
                await event.bot.send_message(chat_id=chat.id, text=welcome_text, reply_markup=kb, parse_mode="HTML")
            except Exception as e:
                logger.warning("Не удалось отправить приветствие в группу %d: %s", chat.id, e)


# -------------------------------------------------------------------------
# КОМАНДЫ СТАРТА И ВЫБОРА ГРУППЫ
# -------------------------------------------------------------------------

@router.message(Command("set_group"))
async def cmd_set_group(message: Message, database: Database = default_db):
    """
    Команда привязки или изменения группы колледжа для текущего чата или пользователя.
    В групповых чатах настраивать группу могут только администраторы.
    """
    is_group = message.chat.type in ("group", "supergroup")
    if is_group:
        is_admin = await is_group_admin(message.bot, message.chat.id, message.from_user.id)
        if not is_admin:
            await message.answer("⚠️ Настраивать учебную группу для этого чата могут только администраторы.")
            return

    specialties = await database.get_all_specialties()
    if not specialties:
        await message.answer("⚠️ Список специальностей сейчас пуст. Пожалуйста, попробуйте через минуту.")
        return

    kb = get_specialties_inline_keyboard(specialties, show_back_to_menu=not is_group)
    chat_label = f"для чата <b>{message.chat.title}</b>" if is_group and message.chat.title else ""
    await message.answer(
        f"🎯 <b>Выбор учебной группы {chat_label}</b> (Шаг 1 из 2):\n\n"
        "Выберите вашу <b>специальность</b>:",
        reply_markup=kb,
        parse_mode="HTML"
    )


@router.message(CommandStart())
async def cmd_start(message: Message, database: Database = default_db):
    """
    Хэндлер команды /start.
    В групповом чате: приветствие группы и статус привязки.
    В личном чате: регистрация студента и главное меню.
    """
    is_group = message.chat.type in ("group", "supergroup")

    if is_group:
        chat_id = message.chat.id
        chat_data = await database.get_user(chat_id)
        if not chat_data or not chat_data.get("group_id"):
            is_admin = await is_group_admin(message.bot, chat_id, message.from_user.id)
            if not is_admin:
                await message.answer(
                    "👋 Привет! Этот чат пока не привязан к группе колледжа КТМУ.\n"
                    "Попросите администратора чата настроить группу командой /set_group."
                )
                return
            await cmd_set_group(message, database)
            return
        else:
            group_name = chat_data.get("group_name", "не указана")
            await message.answer(
                f"👋 Этот чат привязан к группе: <b>{group_name}</b>.\n\n"
                "🌅 Расписание автоматически отправляется сюда <b>каждое утро к 08:00</b>.\n\n"
                "Команды для просмотра:\n"
                "• /today — Расписание на сегодня\n"
                "• /tomorrow — Расписание на завтра\n"
                "• /week — Расписание на неделю\n"
                "• /set_group — Сменить учебную группу",
                parse_mode="HTML"
            )
            return

    # Личные сообщения (private chat)
    user_id = message.from_user.id
    username = message.from_user.username
    first_name = message.from_user.first_name

    # Сохраняем/обновляем активность пользователя
    await database.touch_user(user_id, username=username, first_name=first_name)
    user = await database.get_user(user_id)

    if not user or not user.get("group_id"):
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
        is_admin = config.is_admin(user_id)
        kb = get_main_reply_keyboard(notifications_enabled=notif, is_admin=is_admin)
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
    is_group = callback.message.chat.type in ("group", "supergroup")
    if is_group:
        try:
            await callback.message.edit_text(
                "🏠 <b>Меню группы КТМУ</b>\n\n"
                "Используйте команды чата для просмотра расписания:\n"
                "• /today — Расписание на сегодня\n"
                "• /tomorrow — Расписание на завтра\n"
                "• /week — Расписание на неделю\n"
                "• /set_group — Настройка учебной группы",
                parse_mode="HTML"
            )
        except Exception:
            pass
        await callback.answer()
        return

    user_id = callback.from_user.id
    user = await database.get_user(user_id)
    notif = user.get("notifications_enabled", True) if user else True
    is_admin = config.is_admin(user_id)
    kb = get_main_reply_keyboard(notifications_enabled=notif, is_admin=is_admin)

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
    is_group = callback.message.chat.type in ("group", "supergroup")
    if is_group:
        is_admin = await is_group_admin(callback.bot, callback.message.chat.id, callback.from_user.id)
        if not is_admin:
            await callback.answer("⚠️ Только администраторы чата могут выбирать группу!", show_alert=True)
            return

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

    group_name = group["group_name"]
    relative_url = group["relative_url"]
    full_group_url = f"{config.BASE_URL.rstrip('/')}{relative_url}"

    is_group = callback.message.chat.type in ("group", "supergroup")
    if is_group:
        is_admin = await is_group_admin(callback.bot, callback.message.chat.id, callback.from_user.id)
        if not is_admin:
            await callback.answer("⚠️ Только администраторы чата могут выбирать группу!", show_alert=True)
            return

        chat_id = callback.message.chat.id
        chat_title = callback.message.chat.title or "Групповой чат"

        # Сохраняем группу для группового чата в SQLite
        await database.upsert_user(
            user_id=chat_id,
            group_id=group_id,
            group_url=full_group_url,
            group_name=group_name,
            notifications_enabled=True,
            username=None,
            first_name=chat_title,
        )

        # Фоновый мгновенный прогрев расписания
        asyncio.create_task(timetable_parser.preload_group_schedule(group_id, full_group_url))

        await callback.message.edit_text(
            f"✅ <b>Чат успешно привязан к группе:</b> <code>{group_name}</code>\n"
            f"🔗 Ссылка на расписание: <a href='{full_group_url}'>{relative_url}</a>\n\n"
            "🌅 <b>Каждое утро к 08:00</b> бот будет автоматически отправлять сюда актуальное расписание на текущий день!\n\n"
            "Доступные команды в чате:\n"
            "• /today — Расписание на сегодня\n"
            "• /tomorrow — Расписание на завтра\n"
            "• /week — Расписание на неделю\n"
            "• /set_group — Настройка учебной группы",
            parse_mode="HTML",
            disable_web_page_preview=True
        )
        await callback.answer("Группа для чата успешно сохранена!")
        return

    # Личные сообщения (private chat)
    user_id = callback.from_user.id
    username = callback.from_user.username
    first_name = callback.from_user.first_name

    # Сохраняем пользователя в SQLite с данными профиля
    await database.upsert_user(
        user_id=user_id,
        group_id=group_id,
        group_url=full_group_url,
        group_name=group_name,
        notifications_enabled=True,
        username=username,
        first_name=first_name,
    )

    # Фоновый мгновенный прогрев расписания для выбранной группы!
    asyncio.create_task(timetable_parser.preload_group_schedule(group_id, full_group_url))

    # Удаляем inline-меню выбора
    try:
        await callback.message.delete()
    except Exception:
        pass

    # Отправляем подтверждение с главным меню (с учетом прав администратора)
    is_admin = config.is_admin(user_id)
    kb = get_main_reply_keyboard(notifications_enabled=True, is_admin=is_admin)
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
    """Вспомогательная функция проверки привязки группы у пользователя или чата."""
    is_group = message.chat.type in ("group", "supergroup")
    target_id = message.chat.id if is_group else message.from_user.id
    user = await database.get_user(target_id)
    if not user or not user.get("group_id"):
        if is_group:
            await message.answer(
                "⚠️ Для этого чата еще не выбрана учебная группа колледжа.\n"
                "Администратор может привязать группу командой /set_group."
            )
        else:
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
    is_group = callback.message.chat.type in ("group", "supergroup")
    target_id = callback.message.chat.id if is_group else callback.from_user.id
    user = await database.get_user(target_id)
    if not user or not user.get("group_id"):
        prompt = "Для этого чата еще не выбрана группа: /set_group" if is_group else "Сначала выберите группу: /start"
        await callback.answer(prompt, show_alert=True)
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
    is_admin = config.is_admin(user_id)
    kb = get_main_reply_keyboard(notifications_enabled=new_status, is_admin=is_admin)

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


def get_help_section_text(section: str) -> str:
    """
    Формирует подробный и структурированный текст для каждого раздела справки и инструкции.
    """
    if section == "student":
        return (
            "📱 <b>Инструкция для студента (Личные сообщения):</b>\n\n"
            "1️⃣ <b>Выбор группы:</b>\n"
            "• При первом запуске (/start) выберите специальность и группу.\n"
            "• Сменить группу можно в любой момент кнопкой «⚙️ Сменить группу» или командой /change_group.\n\n"
            "2️⃣ <b>Просмотр расписания:</b>\n"
            "• Кнопка «📅 На сегодня» (/today) — расписание на текущий день.\n"
            "• <i>Умный режим:</i> если пары сегодня уже завершились (например, время 16:00, а последняя пара была до 15:30), "
            "бот автоматически покажет расписание на завтра и предложит кнопку «⏪ Показать прошедшее за сегодня».\n"
            "• Кнопка «📆 На завтра» (/tomorrow) — расписание на следующий день.\n"
            "• Кнопка «🗓 На неделю» (/week) — полное расписание с понедельника по субботу.\n\n"
            "3️⃣ <b>Уведомления:</b>\n"
            "• <b>08:00</b> каждое утро — свежее утреннее расписание на день.\n"
            "• <b>20:00</b> каждый вечер — расписание на завтрашний день.\n"
            "• В течение дня — напоминания за 10 минут до начала каждой пары.\n"
            "• Уведомления можно выключить/включить кнопкой «🔔 Уведомления» или командой /notifications.\n\n"
            "👨‍💻 <i>Разработчик бота: @yapsychokid</i>"
        )
    elif section == "group":
        return (
            "👥 <b>Инструкция: Как добавить бота в группу (беседу):</b>\n\n"
            "Вы можете добавить бота в общую беседу вашей студенческой группы, чтобы расписание приходило всем сразу!\n\n"
            "<b>Шаг 1: Добавление бота</b>\n"
            "• Откройте чат вашей группы в Telegram.\n"
            "• Нажмите на название чата ➔ «Добавить участников» ➔ найдите бота по юзернейму и добавьте его.\n"
            "• (Либо откройте профиль бота в Telegram и нажмите «Добавить в группу»).\n\n"
            "<b>Шаг 2: Назначение прав</b>\n"
            "• Назначьте бота <b>администратором</b> группы (либо убедитесь, что боту разрешено читать сообщения в чате).\n\n"
            "<b>Шаг 3: Привязка учебной группы</b>\n"
            "• Администратор чата должен отправить команду: /set_group\n"
            "• В появившемся меню выберите вашу специальность и группу колледжа.\n\n"
            "🎉 <b>Готово! Что будет дальше:</b>\n"
            "• Бот будет <b>каждое утро ровно к 08:00</b> автоматически отправлять расписание на сегодня прямо в чат!\n"
            "• Любой студент в группе может в любой момент написать /today, /tomorrow или /week.\n"
            "• Спам перед каждой парой в группе отключен — только утренняя сводка к 8 утра.\n\n"
            "👨‍💻 <i>Разработчик бота: @yapsychokid</i>"
        )
    elif section == "commands":
        return (
            "📋 <b>Полный список команд бота:</b>\n\n"
            "• /start — Главное меню и запуск\n"
            "• /today — Расписание на сегодня (с автопереходом на завтра после окончания пар)\n"
            "• /tomorrow — Расписание на завтра\n"
            "• /week — Расписание на всю учебную неделю\n"
            "• /set_group — Настроить/сменить группу (работает и в ЛС, и в беседах)\n"
            "• /change_group — Сменить личную учебную группу\n"
            "• /notifications — Включение / выключение автоматических уведомлений\n"
            "• /help — Инструкция и справка по работе бота\n"
            "• /author — Связь с автором (@yapsychokid)\n\n"
            "👑 <i>Команда администратора бота:</i> /admin\n"
            "🌐 <i>Источник данных:</i> <a href='https://timetable-ktmu.ru/'>timetable-ktmu.ru</a>"
        )
    else:  # "main"
        return (
            "📖 <b>Как пользоваться ботом расписания КТМУ:</b>\n\n"
            "Этот бот создан для студентов КТМУ и умеет быстро отдавать актуальное расписание пар, "
            "присылать утренние и вечерние рассылки, а также работать в учебных беседах!\n\n"
            "🔹 <b>В личных сообщениях:</b>\n"
            "Используйте удобные кнопки главного меню под клавиатурой для быстрого просмотра на сегодня, "
            "завтра и неделю. Бот сам понимает, когда пары кончились, и предлагает завтрашний день.\n\n"
            "🔹 <b>В группах и беседах:</b>\n"
            "Добавьте бота в чат своей учебной группы, напишите /set_group — и бот будет <b>каждое утро к 08:00</b> "
            "присылать готовое расписание вашей группы всем однокурсникам!\n\n"
            "👇 <i>Выберите раздел ниже для подробной инструкции:</i>"
        )


@router.message(Command("help"))
@router.message(Command("guide"))
@router.message(F.text == "📖 Инструкция")
async def cmd_help(message: Message):
    """
    Интерактивная справка и подробная инструкция по использованию бота.
    """
    is_group = message.chat.type in ("group", "supergroup")
    section = "group" if is_group else "main"
    text = get_help_section_text(section)
    kb = get_help_inline_keyboard(current_section=section, show_back_to_menu=not is_group)
    await message.answer(text, reply_markup=kb, parse_mode="HTML", disable_web_page_preview=True)


@router.callback_query(HelpCallback.filter())
async def cb_help_section(callback: CallbackQuery, callback_data: HelpCallback):
    """
    Переключение разделов справки по нажатию инлайн-кнопок.
    """
    section = callback_data.section
    is_group = callback.message.chat.type in ("group", "supergroup")
    text = get_help_section_text(section)
    kb = get_help_inline_keyboard(current_section=section, show_back_to_menu=not is_group)
    try:
        await callback.message.edit_text(text, reply_markup=kb, parse_mode="HTML", disable_web_page_preview=True)
    except Exception:
        pass
    await callback.answer()


# -------------------------------------------------------------------------
# ПАНЕЛЬ АДМИНИСТРАТОРА (ID: 870396858)
# -------------------------------------------------------------------------

@router.message(Command("admin"))
@router.message(F.text == "👑 Админ-панель")
async def cmd_admin_panel(message: Message, state: FSMContext, database: Database = default_db):
    """
    Точка входа в админ-панель бота. Доступна только администраторам.
    """
    user_id = message.from_user.id
    if not config.is_admin(user_id):
        await message.answer("⛔ <b>Доступ запрещен:</b> у вас нет прав администратора бота.", parse_mode="HTML")
        return

    await state.clear()
    stats = await database.get_admin_stats()
    kb = get_admin_main_inline_keyboard()

    text = (
        "👑 <b>Панель администратора КТМУ</b>\n\n"
        f"👤 <b>Администратор:</b> ID <code>{user_id}</code>\n"
        f"📊 <b>Всего подключений:</b> <code>{stats['total_users']}</code>\n"
        f"👤 <b>Личных диалогов:</b> <code>{stats.get('private_users', stats['total_users'])}</code>\n"
        f"💬 <b>Групповых чатов:</b> <code>{stats.get('group_chats', 0)}</code>\n"
        f"🎓 <b>Выбрали группу:</b> <code>{stats['with_group']}</code>\n"
        f"🔔 <b>Включили рассылки:</b> <code>{stats['notifications_on']}</code>\n"
        f"🏛 <b>Групп колледжа в базе:</b> <code>{stats['total_groups']}</code>\n\n"
        "Выберите необходимое действие в меню ниже:"
    )
    await message.answer(text, reply_markup=kb, parse_mode="HTML")


@router.callback_query(AdminCallback.filter())
async def cb_admin_actions(
    callback: CallbackQuery,
    callback_data: AdminCallback,
    state: FSMContext,
    database: Database = default_db,
):
    """
    Обработка нажатий на кнопки в админ-панели.
    """
    user_id = callback.from_user.id
    if not config.is_admin(user_id):
        await callback.answer("⛔ Доступ запрещен!", show_alert=True)
        return

    action = callback_data.action

    if action == "menu":
        await state.clear()
        stats = await database.get_admin_stats()
        kb = get_admin_main_inline_keyboard()
        text = (
            "👑 <b>Панель администратора КТМУ</b>\n\n"
            f"👤 <b>Администратор:</b> ID <code>{user_id}</code>\n"
            f"📊 <b>Всего подключений:</b> <code>{stats['total_users']}</code>\n"
            f"👤 <b>Личных диалогов:</b> <code>{stats.get('private_users', stats['total_users'])}</code>\n"
            f"💬 <b>Групповых чатов:</b> <code>{stats.get('group_chats', 0)}</code>\n"
            f"🎓 <b>Выбрали группу:</b> <code>{stats['with_group']}</code>\n"
            f"🔔 <b>Включили рассылки:</b> <code>{stats['notifications_on']}</code>\n"
            f"🏛 <b>Групп колледжа в базе:</b> <code>{stats['total_groups']}</code>\n\n"
            "Выберите необходимое действие в меню ниже:"
        )
        await callback.message.edit_text(text, reply_markup=kb, parse_mode="HTML")
        await callback.answer()

    elif action == "stats":
        stats = await database.get_admin_stats()
        kb = get_admin_back_inline_keyboard()
        total = max(1, stats['total_users'])
        pct_group = (stats['with_group'] / total) * 100
        pct_notif = (stats['notifications_on'] / total) * 100
        text = (
            "📊 <b>Детальная статистика бота</b>\n\n"
            f"👥 <b>Всего в базе:</b> <code>{stats['total_users']}</code>\n"
            f"👤 <b>Личных диалогов (студенты):</b> <code>{stats.get('private_users', 0)}</code>\n"
            f"💬 <b>Групповых чатов (классы/группы):</b> <code>{stats.get('group_chats', 0)}</code>\n"
            f"🎓 <b>Выбрали группу:</b> <code>{stats['with_group']}</code> ({pct_group:.1f}%)\n"
            f"🔔 <b>Подписчиков на рассылки:</b> <code>{stats['notifications_on']}</code> ({pct_notif:.1f}%)\n"
            f"🏛 <b>Групп в базе данных:</b> <code>{stats['total_groups']}</code>\n"
        )
        await callback.message.edit_text(text, reply_markup=kb, parse_mode="HTML")
        await callback.answer()

    elif action == "groups":
        top_groups = await database.get_top_groups(limit=25)
        kb = get_admin_back_inline_keyboard()
        if not top_groups:
            text = "👥 <b>Студенты по группам:</b>\n\nПока никто не выбрал учебную группу."
        else:
            lines = ["👥 <b>Топ групп по количеству зарегистрированных студентов:</b>\n"]
            for idx, item in enumerate(top_groups, 1):
                lines.append(f"{idx}. <code>{item['group_name']}</code> — <b>{item['count']}</b> чел.")
            text = "\n".join(lines)
        await callback.message.edit_text(text, reply_markup=kb, parse_mode="HTML")
        await callback.answer()

    elif action == "users":
        recent_users = await database.get_recent_users(limit=20)
        kb = get_admin_back_inline_keyboard()
        if not recent_users:
            text = "📋 <b>Список пользователей:</b>\n\nБаза пользователей пуста."
        else:
            lines = ["📋 <b>Последние пользователи бота (до 20):</b>\n"]
            for idx, u in enumerate(recent_users, 1):
                uid = u["user_id"]
                name = u.get("first_name") or "Без имени"
                uname = f"@{u['username']}" if u.get("username") else f"ID {uid}"
                grp = u.get("group_name") or "Группа не выбрана"
                notif = "🔔" if u.get("notifications_enabled") else "🔕"
                created = str(u.get("created_at", ""))[:16]
                lines.append(f"{idx}. <b>{name}</b> ({uname}) — <code>{grp}</code> {notif} <i>[{created}]</i>")
            text = "\n".join(lines)
        await callback.message.edit_text(text, reply_markup=kb, parse_mode="HTML")
        await callback.answer()

    elif action == "warmup":
        await callback.answer("⚡ Запущен прогрев кэша...", show_alert=False)
        asyncio.create_task(timetable_parser.preload_active_groups_schedules(database))
        kb = get_admin_back_inline_keyboard()
        await callback.message.edit_text(
            "⚡ <b>Фоновый прогрев расписания успешно запущен!</b>\n\n"
            "Все активные группы колледжа сейчас предзагружаются в SQLite и оперативную память RAM. "
            "Студенты будут получать расписание мгновенно!",
            reply_markup=kb,
            parse_mode="HTML",
        )

    elif action == "clearcache":
        deleted = await database.clear_timetable_cache()
        timetable_parser.clear_ram_cache()
        kb = get_admin_back_inline_keyboard()
        await callback.message.edit_text(
            f"🗑 <b>Кэш расписания успешно очищен!</b>\n\n"
            f"Удалено записей из базы данных: <code>{deleted}</code>\n"
            "Оперативный RAM-кэш сброшен.",
            reply_markup=kb,
            parse_mode="HTML",
        )
        await callback.answer("Кэш очищен!")

    elif action == "close":
        try:
            await callback.message.delete()
        except Exception:
            pass
        await callback.answer("Панель закрыта")

    elif action == "broadcast":
        await state.set_state(AdminStates.waiting_for_broadcast_text)
        kb = get_broadcast_cancel_inline_keyboard()
        await callback.message.edit_text(
            "📢 <b>Массовая рассылка сообщений</b>\n\n"
            "Отправьте боту текст (или сообщение с фото/медиа), которое необходимо разослать "
            "<b>всем зарегистрированным пользователям</b>.\n\n"
            "<i>Для отмены нажмите кнопку ниже:</i>",
            reply_markup=kb,
            parse_mode="HTML",
        )
        await callback.answer()


@router.message(AdminStates.waiting_for_broadcast_text)
async def process_admin_broadcast(message: Message, state: FSMContext, database: Database = default_db):
    """
    Выполняет рассылку присланного админом сообщения всем пользователям.
    """
    user_id = message.from_user.id
    if not config.is_admin(user_id):
        await state.clear()
        return

    await state.clear()

    user_ids = await database.get_all_user_ids()
    total_users = len(user_ids)
    if total_users == 0:
        await message.answer("⚠️ В базе данных пока нет зарегистрированных пользователей.")
        return

    status_msg = await message.answer(
        f"🚀 <b>Начало рассылки:</b> получателей {total_users}...\nПожалуйста, подождите.",
        parse_mode="HTML",
    )

    success_count = 0
    blocked_count = 0
    error_count = 0

    from aiogram.exceptions import TelegramForbiddenError

    for uid in user_ids:
        try:
            await message.send_copy(chat_id=uid)
            success_count += 1
        except TelegramForbiddenError:
            blocked_count += 1
            await database.set_user_notifications(uid, False)
        except Exception as e:
            logger.warning("Ошибка при рассылке пользователю %d: %s", uid, e)
            error_count += 1
        await asyncio.sleep(0.04)  # 25 сообщений в секунду

    kb = get_admin_back_inline_keyboard()
    await status_msg.edit_text(
        "✅ <b>Рассылка успешно завершена!</b>\n\n"
        f"👥 Всего получателей: <code>{total_users}</code>\n"
        f"📤 Успешно доставлено: <code>{success_count}</code>\n"
        f"🚫 Заблокировали бота: <code>{blocked_count}</code>\n"
        f"❌ Ошибок отправки: <code>{error_count}</code>",
        reply_markup=kb,
        parse_mode="HTML",
    )
