import asyncio
import logging
import os
import shutil
import sqlite3
from datetime import date, datetime, time, timedelta
from typing import Any, Optional
from zoneinfo import ZoneInfo

from aiogram import Bot, F, Router
from aiogram.enums import ChatAction
from aiogram.filters import Command, CommandStart, StateFilter
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import (
    BufferedInputFile,
    CallbackQuery,
    ChatMemberUpdated,
    FSInputFile,
    InputMediaPhoto,
    Message,
)

from config import config
from database import Database, db as default_db
from image_generator import render_schedule_image
from keyboards import (
    BELLS_TIMETABLE,
    AdminCallback,
    AdminStarostaApproveCallback,
    BellsCallback,
    CampusCallback,
    ExamsCallback,
    GroupCallback,
    GroupChatLinkCallback,
    HelpCallback,
    HomeworkCallback,
    HwDateCallback,
    HwSubjectCallback,
    LanguageCallback,
    LeadTimeCallback,
    MyTeacherCallback,
    NavigationCallback,
    NoteDateCallback,
    NotePairCallback,
    NotesCallback,
    NotificationSettingCallback,
    ScheduleNavCallback,
    SkipPairCallback,
    SpecialtyCallback,
    StarostaCallback,
    SubjectCallback,
    SubgroupCallback,
    TeacherChoiceCallback,
    build_webapp_url,
    get_add_to_group_keyboard,
    get_admin_back_inline_keyboard,
    get_admin_main_inline_keyboard,
    get_admin_starosta_decision_keyboard,
    get_admin_starosta_remove_keyboard,
    get_bells_inline_keyboard,
    get_broadcast_cancel_inline_keyboard,
    get_campus_inline_keyboard,
    get_evening_time_selection_keyboard,
    get_exams_delete_keyboard,
    get_exams_list_keyboard,
    get_group_chat_link_keyboard,
    get_group_menu_keyboard,
    get_group_subjects_inline_keyboard,
    get_groups_inline_keyboard,
    get_help_inline_keyboard,
    get_homework_date_suggestions_keyboard,
    get_homework_delete_keyboard,
    get_homework_list_keyboard,
    get_homework_subjects_inline_keyboard,
    get_language_inline_keyboard,
    get_main_reply_keyboard,
    get_morning_time_selection_keyboard,
    get_my_teachers_keyboard,
    get_note_date_quick_keyboard,
    get_note_pair_quick_keyboard,
    get_notification_lead_time_keyboard,
    get_notification_settings_keyboard,
    get_schedule_bonch_keyboard,
    get_schedule_nav_keyboard,
    get_settings_info_keyboard,
    get_skip_pair_keyboard,
    get_specialties_inline_keyboard,
    get_starosta_inline_keyboard,
    get_subgroup_selection_keyboard,
    get_teacher_cancel_keyboard,
    get_teacher_search_choice_keyboard,
    get_user_notes_delete_keyboard,
    get_user_notes_keyboard,
    get_webapp_inline_keyboard,
)
from i18n import get_text
from image_generator import MONTHS_RU
from timetable_parser import (
    MONTH_NAMES_GENITIVE,
    filter_lessons_by_subgroup,
    format_day_schedule_message,
    format_week_schedule_messages,
    generate_ics_calendar,
    get_current_pair_status_info,
    timetable_parser,
)

logger = logging.getLogger(__name__)

router = Router(name="main_router")


async def _get_bot_username(bot: Optional[Bot]) -> str:
    """Безопасно получает username бота или возвращает fallback."""
    if not bot:
        return "schedulektmubot"
    try:
        me = await bot.get_me()
        if hasattr(me, "username") and me.username:
            return me.username
    except Exception:
        pass
    return "schedulektmubot"


async def send_chat_action_safe(
    bot: Optional[Bot],
    chat_id: int,
    action: str = ChatAction.TYPING,
    message_thread_id: Optional[int] = None,
) -> None:
    """Безопасно отправляет индикатор действия (typing, upload_photo) в чат, игнорируя сетевые ошибки."""
    if not bot:
        return
    try:
        kwargs: dict[str, Any] = {"chat_id": chat_id, "action": action}
        if message_thread_id is not None:
            kwargs["message_thread_id"] = message_thread_id
        await bot.send_chat_action(**kwargs)
    except Exception:
        pass


# Горячий RAM-кэш file_id изображений расписания: (group_id, date_str) -> file_id
_ram_image_cache: dict[tuple[str, str], str] = {}


def clear_ram_image_cache() -> None:
    """Очищает оперативный кэш Telegram file_id изображений расписания."""
    _ram_image_cache.clear()



class AdminStates(StatesGroup):
    waiting_for_broadcast_text = State()
    waiting_for_db_file = State()


class TeacherSearchStates(StatesGroup):
    waiting_for_teacher_name = State()


class HomeworkStates(StatesGroup):
    waiting_for_subject = State()
    waiting_for_due_date = State()
    waiting_for_task_text = State()


class GroupChatLinkStates(StatesGroup):
    waiting_for_url = State()


class CampusStates(StatesGroup):
    waiting_for_search_query = State()


class ExamStates(StatesGroup):
    waiting_for_subject = State()
    waiting_for_date = State()
    waiting_for_time = State()
    waiting_for_room = State()
    waiting_for_teacher = State()


class StarostaDeputyStates(StatesGroup):
    waiting_for_deputy_input = State()


class NoteStates(StatesGroup):
    waiting_for_text = State()
    waiting_for_date = State()
    waiting_for_pair = State()


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

@router.message(Command("set_topic"))
async def cmd_set_topic(message: Message, database: Database = default_db):
    """
    Команда привязки учебной группы к теме супергруппы (Telegram Forum Topic).
    Считывает chat_id и message_thread_id.
    """
    if message.chat.type not in ("group", "supergroup"):
        bot_username = await _get_bot_username(message.bot)
        kb = get_add_to_group_keyboard(bot_username)
        await message.answer(
            "⚠️ Команда /set_topic предназначена для использования в групповых чатах и темах (форумах).\n\n"
            "Нажмите кнопку ниже, чтобы легко добавить бота в вашу беседу или тему:",
            reply_markup=kb,
            parse_mode="HTML"
        )
        return

    is_admin = await is_group_admin(message.bot, message.chat.id, message.from_user.id)
    if not is_admin:
        await message.answer("⚠️ Настраивать учебную группу для этой темы могут только администраторы чата.")
        return

    specialties = await database.get_all_specialties()
    if not specialties:
        await message.answer("⚠️ Список специальностей сейчас пуст. Пожалуйста, попробуйте через минуту.")
        return

    kb = get_specialties_inline_keyboard(specialties, show_back_to_menu=False)
    thread_id = getattr(message, "message_thread_id", None)
    topic_label = f"для темы #{thread_id}" if thread_id else f"для чата <b>{message.chat.title}</b>"
    await message.answer(
        f"🎯 <b>Выбор учебной группы {topic_label}</b> (Шаг 1 из 2):\n\n"
        "Выберите вашу <b>специальность</b>:",
        reply_markup=kb,
        parse_mode="HTML"
    )


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


@router.message(CommandStart(), StateFilter("*"))
async def cmd_start(message: Message, state: Optional[FSMContext] = None, database: Database = default_db):
    """
    Хэндлер команды /start.
    В групповом чате: приветствие группы и статус привязки.
    В личном чате: регистрация студента и главное меню.
    """
    if state:
        await state.clear()
    is_group = message.chat.type in ("group", "supergroup")

    if is_group:
        chat_id = message.chat.id
        thread_id = getattr(message, "message_thread_id", None)
        chat_data = await database.get_chat(chat_id, thread_id)
        if not chat_data and thread_id is not None:
            chat_data = await database.get_chat(chat_id, None)
        if not chat_data:
            chat_data = await database.get_user(chat_id)

        if not chat_data or not chat_data.get("group_id"):
            is_admin = await is_group_admin(message.bot, chat_id, message.from_user.id)
            if not is_admin:
                await message.answer(
                    "👋 Привет! Этот чат/тема пока не привязана к группе колледжа КТМУ.\n"
                    "Попросите администратора чата настроить группу командой /set_topic или /set_group."
                )
                return
            await cmd_set_group(message, database)
            return
        else:
            group_name = chat_data.get("group_name", "не указана")
            await message.answer(
                f"👋 Этот чат/тема привязана к группе: <b>{group_name}</b>.\n\n"
                "🌅 Расписание автоматически отправляется сюда <b>каждое утро к 08:00</b> и <b>вечером в 20:00</b>.\n\n"
                "Команды для просмотра:\n"
                "• /today — Расписание на сегодня\n"
                "• /tomorrow — Расписание на завтра\n"
                "• /week — Расписание на неделю\n"
                "• /set_topic — Привязать эту тему к группе\n"
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
        # Шаг 0: Выбор языка (как в Bonch Bot)
        await message.answer(
            "👋 <b>Добро пожаловать в бот расписания КТМУ!</b>\n"
            "Welcome to KTMU Schedule Bot!\n\n"
            "Выберите язык / Choose language:",
            reply_markup=get_language_inline_keyboard(),
            parse_mode="HTML"
        )
    else:
        group_name = user.get("group_name", "не указана")
        notif = user.get("notifications_enabled", True)
        lang = user.get("language", "ru")
        is_admin = config.is_admin(user_id)
        kb = get_main_reply_keyboard(notifications_enabled=notif, is_admin=is_admin, lang=lang)
        welcome_text = (
            f"👋 С возвращением! Ваша группа: <b>{group_name}</b>.\n\n"
            "Воспользуйтесь кнопками меню ниже для просмотра расписания и домашних заданий:"
            if lang == "ru"
            else f"👋 Welcome back! Your group: <b>{group_name}</b>.\n\n"
            "Use the menu buttons below to view schedules and homework:"
        )
        await message.answer(
            welcome_text,
            reply_markup=kb,
            parse_mode="HTML"
        )


@router.callback_query(LanguageCallback.filter())
async def cb_select_language(
    callback: CallbackQuery, callback_data: LanguageCallback, database: Database = default_db
):
    """
    Обработчик выбора языка интерфейса (RU / EN).
    """
    lang = callback_data.lang
    user_id = callback.from_user.id
    await database.set_user_language(user_id, lang)

    user = await database.get_user(user_id)
    if not user or not user.get("group_id"):
        specialties = await database.get_all_specialties()
        if not specialties:
            await callback.message.edit_text(
                "⚠️ Список специальностей сейчас пуст. Пожалуйста, попробуйте через минуту."
            )
            await callback.answer()
            return

        kb = get_specialties_inline_keyboard(specialties, show_back_to_menu=False)
        prompt = (
            "🇷🇺 <b>Русский язык выбран.</b>\n\n"
            "Пожалуйста, выберите вашу <b>специальность</b> (Шаг 1 из 2):"
            if lang == "ru"
            else "🇬🇧 <b>English language selected.</b>\n\n"
            "Please select your <b>department / specialty</b> (Step 1 of 2):"
        )
        await callback.message.edit_text(prompt, reply_markup=kb, parse_mode="HTML")
    else:
        group_name = user.get("group_name", "не указана")
        notif = user.get("notifications_enabled", True)
        is_admin = config.is_admin(user_id)
        kb = get_main_reply_keyboard(notifications_enabled=notif, is_admin=is_admin, lang=lang)
        msg_text = (
            f"🇷🇺 Язык успешно переключен на русский!\nГруппа: <b>{group_name}</b>."
            if lang == "ru"
            else f"🇬🇧 Language switched to English!\nGroup: <b>{group_name}</b>."
        )
        try:
            await callback.message.delete()
        except Exception:
            pass
        await callback.message.answer(msg_text, reply_markup=kb, parse_mode="HTML")
    await callback.answer()


@router.message(Command("lang"))
@router.message(Command("language"))
@router.message(F.text.in_({"🌐 Язык", "🌐 Language"}))
async def cmd_language(message: Message):
    """
    Команда и кнопка переключения языка.
    """
    await message.answer(
        "🌐 <b>Выберите язык интерфейса / Choose interface language:</b>",
        reply_markup=get_language_inline_keyboard(),
        parse_mode="HTML"
    )


@router.message(Command("change_group"))
@router.message(F.text.in_({"⚙️ Сменить группу", "⚙️ Change group"}))
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
async def cb_back_to_main_menu(
    callback: CallbackQuery, state: Optional[FSMContext] = None, database: Database = default_db
):
    """
    Кнопка «Назад в главное меню».
    """
    if state:
        await state.clear()
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
        thread_id = getattr(callback.message, "message_thread_id", None)
        chat_title = callback.message.chat.title or "Групповой чат"

        # 1. Сохраняем группу и тему в таблицу chats
        await database.upsert_chat(
            chat_id=chat_id,
            message_thread_id=thread_id,
            group_id=group_id,
            group_url=full_group_url,
            notifications_enabled=True,
        )

        # 2. Также обновляем legacy запись в таблице users для обратной совместимости
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

        target_title = f"Тема #{thread_id}" if thread_id else "Чат"
        await callback.message.edit_text(
            f"✅ <b>{target_title} успешно привязан(а) к группе:</b> <code>{group_name}</code>\n"
            f"🔗 Ссылка на расписание: <a href='{full_group_url}'>{relative_url}</a>\n\n"
            "🔔 <b>Авто-уведомления включены:</b>\n"
            "• В 20:00 — рассылка расписания на завтра\n"
            "• В 08:00 — утреннее расписание на сегодня\n"
            "• В точное время начала пары — напоминание о начале занятия\n"
            "• Каждые 15 минут — мониторинг замен и изменений расписания!\n\n"
            "Доступные команды в чате:\n"
            "• /today — Расписание на сегодня\n"
            "• /tomorrow — Расписание на завтра\n"
            "• /week — Расписание на неделю\n"
            "• /set_topic — Настройка темы (для топиков)\n"
            "• /set_group — Настройка учебной группы",
            parse_mode="HTML",
            disable_web_page_preview=True
        )
        await callback.answer("Группа для чата/темы успешно сохранена!")
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

    # Предлагаем настроить предварительные напоминания перед парами (Bonch Bot style)
    lang = await database.get_user_language(user_id)
    lead_kb = get_notification_lead_time_keyboard(lang=lang)
    prompt_text = (
        f"✅ <b>Группа успешно сохранена:</b> <code>{group_name}</code>\n"
        f"🔗 Ссылка: <a href='{full_group_url}'>{relative_url}</a>\n\n"
        "Могу присылать уведомление о грядущем занятии за несколько минут до него. Если нужно, скажите за сколько:"
        if lang == "ru"
        else f"✅ <b>Group saved:</b> <code>{group_name}</code>\n"
        f"🔗 Link: <a href='{full_group_url}'>{relative_url}</a>\n\n"
        "I can send a notification before upcoming classes. If needed, please choose how many minutes in advance:"
    )
    await callback.message.answer(
        prompt_text,
        reply_markup=lead_kb,
        parse_mode="HTML",
        disable_web_page_preview=True
    )
    await callback.answer("Группа успешно сохранена!")


@router.callback_query(LeadTimeCallback.filter())
async def cb_select_lead_time(
    callback: CallbackQuery, callback_data: LeadTimeCallback, database: Database = default_db
):
    """
    Сохранение времени предварительного напоминания о парах (5, 10, 15, 20, 30, 45 мин или 0).
    """
    minutes = callback_data.minutes
    user_id = callback.from_user.id
    await database.set_user_lead_minutes(user_id, minutes)

    user = await database.get_user(user_id)
    group_name = user.get("group_name", "не указана") if user else "не указана"
    lang = user.get("language", "ru") if user else "ru"
    notif = user.get("notifications_enabled", True) if user else True
    is_admin = config.is_admin(user_id)

    if minutes == 0:
        lead_desc = (
            "в момент начала занятия"
            if lang == "ru"
            else "at the exact start of class"
        )
    else:
        lead_desc = (
            f"за <b>{minutes} минут</b> до начала пары"
            if lang == "ru"
            else f"<b>{minutes} minutes</b> before class begins"
        )

    try:
        await callback.message.delete()
    except Exception:
        pass

    kb = get_main_reply_keyboard(notifications_enabled=notif, is_admin=is_admin, lang=lang)
    welcome_text = (
        f"⏱ <b>Напоминания настроены:</b> {lead_desc}.\n\n"
        f"Группа: <b>{group_name}</b>.\n"
        "🔔 Ежедневные рассылки в 20:00 (вечер) и в 08:00 (утро) включены.\n\n"
        "Используйте кнопки меню ниже для просмотра расписания и домашних заданий:"
        if lang == "ru"
        else f"⏱ <b>Reminders configured:</b> {lead_desc}.\n\n"
        f"Group: <b>{group_name}</b>.\n"
        "🔔 Daily broadcasts at 20:00 and 08:00 enabled.\n\n"
        "Use the menu buttons below to view schedule and homework:"
    )
    await callback.message.answer(welcome_text, reply_markup=kb, parse_mode="HTML")
    await callback.answer()


# -------------------------------------------------------------------------
# ОСНОВНОЙ ФУНКЦИОНАЛ РАСПИСАНИЯ
# -------------------------------------------------------------------------

async def _get_authorized_user(message: Message, database: Database) -> dict | None:
    """Вспомогательная функция проверки привязки группы у пользователя или чата/темы."""
    is_group = message.chat.type in ("group", "supergroup")
    if is_group:
        chat_id = message.chat.id
        thread_id = getattr(message, "message_thread_id", None)

        # 1. Поиск в таблице chats по связке (chat_id, message_thread_id)
        chat_data = await database.get_chat(chat_id, thread_id)
        if chat_data and chat_data.get("group_id"):
            return chat_data

        # 2. Если вызвано внутри топика, но топик не настроен отдельно — проверяем общий чат (None)
        if thread_id is not None:
            general_chat = await database.get_chat(chat_id, None)
            if general_chat and general_chat.get("group_id"):
                return general_chat

        # 3. Fallback: проверка legacy таблицы users
        legacy_data = await database.get_user(chat_id)
        if legacy_data and legacy_data.get("group_id"):
            return legacy_data

        await message.answer(
            "⚠️ Для этого чата/темы еще не выбрана учебная группа колледжа.\n"
            "Администратор может привязать группу командой /set_topic (в теме) или /set_group."
        )
        return None

    # Личные сообщения
    user = await database.get_user(message.from_user.id)
    if not user or not user.get("group_id"):
        await message.answer(
            "⚠️ Сначала выберите вашу группу с помощью команды /start или /change_group."
        )
        return None
    return user


@router.message(Command("group_menu"), StateFilter("*"))
@router.message(F.text.in_({
    "🎓 Меню группы (ДЗ / Староста)",
    "🎓 Меню группы",
    "🎓 Group menu (HW / Starosta)",
    "🎓 Group menu",
}), StateFilter("*"))
async def cmd_group_menu(message: Message, state: Optional[FSMContext] = None, database: Database = default_db):
    """
    Отдельное подменю группы: ДЗ, Староста, Сменить группу.
    """
    if state:
        await state.clear()
    user = await _get_authorized_user(message, database)
    if not user:
        return

    lang = user.get("language", "ru")
    group_name = user.get("group_name", "не выбрана")
    kb = get_group_menu_keyboard(lang=lang)

    text = (
        f"🎓 <b>Меню группы {group_name}</b>\n\n"
        "Выберите необходимое действие:\n"
        "• <b>📚 ДЗ 📚</b> — просмотр и управление домашними заданиями\n"
        "• <b>💬 Чат группы</b> — официальная беседа группы (TG/VK)\n"
        "• <b>📝 Личные заметки</b> — персональный блокнот студента и дедлайны\n"
        "• <b>🙋‍♂️ Староста 🙋‍♂️</b> — информация о старосте и управление статусом\n"
        "• <b>⚙️ Сменить группу</b> — выбор другой специальности и группы\n"
        "• <b>⬅️ Главное меню</b> — вернуться к просмотру расписания"
        if lang == "ru"
        else f"🎓 <b>Group menu: {group_name}</b>\n\n"
        "Select an action:\n"
        "• <b>📚 Homework 📚</b> — view and manage group homework\n"
        "• <b>💬 Group chat</b> — official group chat link (TG/VK)\n"
        "• <b>📝 Personal notes</b> — personal notepad and deadlines\n"
        "• <b>🙋‍♂️ Starosta 🙋‍♂️</b> — starosta info and status\n"
        "• <b>⚙️ Change group</b> — pick another group\n"
        "• <b>⬅️ Main menu</b> — return to schedule"
    )
    await message.answer(text, reply_markup=kb, parse_mode="HTML")


@router.message(F.text.in_({"⬅️ Главное меню", "⬅️ Main menu", "🏠 Главное меню"}), StateFilter("*"))
async def cmd_back_to_main_menu(message: Message, state: Optional[FSMContext] = None, database: Database = default_db):
    """
    Возврат из подменю группы в основное меню расписания.
    """
    if state:
        await state.clear()
    user_id = message.from_user.id
    user = await database.get_user(user_id)
    lang = user.get("language", "ru") if user else "ru"
    notif = user.get("notifications_enabled", True) if user else True
    is_admin = config.is_admin(user_id)
    kb = get_main_reply_keyboard(notifications_enabled=notif, is_admin=is_admin, lang=lang)

    text = (
        "🏠 <b>Главное меню</b>\n\nИспользуйте кнопки меню ниже для просмотра расписания:"
        if lang == "ru"
        else "🏠 <b>Main menu</b>\n\nUse the buttons below to check your schedule:"
    )
    await message.answer(text, reply_markup=kb, parse_mode="HTML")


async def send_day_schedule_with_image(
    bot: Bot,
    chat_id: int,
    database: Database,
    group_id: str,
    group_name: str,
    target_date: date,
    sched: dict[str, Any],
    message_thread_id: Optional[int] = None,
    wait_message: Optional[Message] = None,
    edit_message: Optional[Message] = None,
    notice_prefix: str = "",
    show_today_past: bool = False,
    is_group: bool = False,
    user_subgroup: int = 0,
    subgroup_overrides: Optional[dict[str, int]] = None,
) -> Optional[Message]:
    """
    Отправляет расписание на день сразу в виде графической карточки-изображения (PNG)
    с интерактивной клавиатурой BonchGo и текстовым описанием.
    Кэширует Telegram file_id (в RAM и SQLite) для мгновенной повторной отправки
    тем же или другим группам/пользователям без перерисовки через Pillow.
    """
    date_str = target_date.isoformat()
    formatted_date = target_date.strftime("%d.%m.%Y")
    sched_text = format_day_schedule_message(
        sched, group_name, user_subgroup=user_subgroup, subgroup_overrides=subgroup_overrides
    )
    full_text = f"{notice_prefix}{sched_text}".strip()

    # Лимит подписи к фото в Telegram — 1024 символа
    if len(full_text) <= 1024:
        caption = full_text
    else:
        caption = f"{notice_prefix}🗓 <b>Расписание группы {group_name}</b> на {formatted_date}".strip()

    webapp_url = build_webapp_url(group_name=group_name, subgroup=user_subgroup)
    kb = get_schedule_bonch_keyboard(
        target_date=target_date,
        show_back_to_menu=not is_group,
        show_today_past=show_today_past,
        show_calendar=True,
        webapp_url=webapp_url,
    )

    # 1. Проверяем кэш file_id (в RAM, затем в БД)
    file_id = _ram_image_cache.get((group_id, date_str))
    if not file_id:
        file_id = await database.get_cached_schedule_image(group_id, date_str)
        if file_id:
            _ram_image_cache[(group_id, date_str)] = file_id

    sent_msg: Optional[Message] = None

    # Попытка отправки через существующий кэш
    if file_id:
        try:
            if edit_message and getattr(edit_message, "photo", None):
                sent_msg = await edit_message.edit_media(
                    InputMediaPhoto(media=file_id, caption=caption, parse_mode="HTML"),
                    reply_markup=kb,
                )
            else:
                kwargs: dict[str, Any] = {
                    "chat_id": chat_id,
                    "photo": file_id,
                    "caption": caption,
                    "reply_markup": kb,
                    "parse_mode": "HTML",
                }
                if message_thread_id is not None:
                    kwargs["message_thread_id"] = message_thread_id
                sent_msg = await bot.send_photo(**kwargs)
                if wait_message:
                    try:
                        await wait_message.delete()
                    except Exception:
                        pass
                if edit_message and not getattr(edit_message, "photo", None):
                    try:
                        await edit_message.delete()
                    except Exception:
                        pass

            if len(full_text) > 1024:
                txt_kwargs: dict[str, Any] = {
                    "chat_id": chat_id,
                    "text": full_text,
                    "parse_mode": "HTML",
                }
                if message_thread_id is not None:
                    txt_kwargs["message_thread_id"] = message_thread_id
                await bot.send_message(**txt_kwargs)

            return sent_msg
        except Exception as e:
            logger.warning("Не удалось отправить сохраненное изображение по file_id (%s): %s. Перерисовываем...", file_id, e)
            _ram_image_cache.pop((group_id, date_str), None)
            file_id = None

    # 2. Если в кэше нет или отправка по file_id не удалась -> генерируем изображение через render_schedule_image
    await send_chat_action_safe(bot, chat_id, ChatAction.UPLOAD_PHOTO, message_thread_id)
    bot_username = await _get_bot_username(bot)
    img_io = render_schedule_image(sched, group_name, bot_username)
    photo_file = BufferedInputFile(
        img_io.getvalue(), filename=f"schedule_{group_name}_{date_str}.png"
    )

    try:
        if edit_message and getattr(edit_message, "photo", None):
            try:
                sent_msg = await edit_message.edit_media(
                    InputMediaPhoto(media=photo_file, caption=caption, parse_mode="HTML"),
                    reply_markup=kb,
                )
            except Exception:
                sent_msg = None

        if not sent_msg:
            kwargs = {
                "chat_id": chat_id,
                "photo": photo_file,
                "caption": caption,
                "reply_markup": kb,
                "parse_mode": "HTML",
            }
            if message_thread_id is not None:
                kwargs["message_thread_id"] = message_thread_id
            sent_msg = await bot.send_photo(**kwargs)
            if wait_message:
                try:
                    await wait_message.delete()
                except Exception:
                    pass
            if edit_message and not getattr(edit_message, "photo", None):
                try:
                    await edit_message.delete()
                except Exception:
                    pass

        if len(full_text) > 1024:
            txt_kwargs = {
                "chat_id": chat_id,
                "text": full_text,
                "parse_mode": "HTML",
            }
            if message_thread_id is not None:
                txt_kwargs["message_thread_id"] = message_thread_id
            await bot.send_message(**txt_kwargs)

        # Сохраняем полученный Telegram file_id в RAM и БД!
        if sent_msg and getattr(sent_msg, "photo", None):
            new_file_id = sent_msg.photo[-1].file_id
            _ram_image_cache[(group_id, date_str)] = new_file_id
            await database.save_cached_schedule_image(group_id, date_str, new_file_id)
            logger.info("Успешно сохранена картинка расписания (file_id: %s) для %s на %s", new_file_id[:16], group_name, date_str)

        return sent_msg
    except Exception as e:
        logger.error("Ошибка отправки расписания с картинкой для %s: %s", group_name, e, exc_info=True)
        # Fallback на текстовое сообщение в крайнем случае
        if wait_message:
            await wait_message.edit_text(full_text, reply_markup=kb, parse_mode="HTML")
        elif edit_message:
            await edit_message.edit_text(full_text, reply_markup=kb, parse_mode="HTML")
        else:
            kwargs = {"chat_id": chat_id, "text": full_text, "reply_markup": kb, "parse_mode": "HTML"}
            if message_thread_id is not None:
                kwargs["message_thread_id"] = message_thread_id
            await bot.send_message(**kwargs)
        return None


@router.message(F.text.in_({"📅 На сегодня", "📅 Today"}))
@router.message(Command("today"))
@router.message(Command("schedule"))
async def handle_schedule_today(message: Message, database: Database = default_db):
    """
    Расписание на сегодня (сразу с графической картинкой).
    Если пары на сегодня уже закончились -> автоматически показывает расписание на завтра
    с кнопкой просмотра прошедшего дня!
    """
    user = await _get_authorized_user(message, database)
    if not user:
        return

    now_dt = get_current_college_time()
    today = now_dt.date()
    group_id = user["group_id"]
    group_name = user["group_name"]
    group_url = user["group_url"]
    is_group = message.chat.type in ("group", "supergroup")
    thread_id = getattr(message, "message_thread_id", None)
    await send_chat_action_safe(message.bot, message.chat.id, ChatAction.TYPING, thread_id)

    wait_msg = await message.answer("⏳ <i>Загружаю расписание...</i>", parse_mode="HTML")

    try:
        today_sched = await timetable_parser.fetch_day_schedule(
            group_id=group_id, group_url=group_url, target_date=today
        )
        lessons = today_sched.get("lessons", [])
        user_sub = user.get("subgroup", 0) if not is_group else 0
        user_overrides = user.get("subgroup_overrides") if not is_group else None

        # Проверяем, завершились ли уже пары на сегодня
        if are_today_pairs_finished(lessons, now_dt):
            tomorrow = today + timedelta(days=1)
            tomorrow_sched = await timetable_parser.fetch_day_schedule(
                group_id=group_id, group_url=group_url, target_date=tomorrow
            )
            notice = (
                "🔔 <b>Пары на сегодня уже закончились!</b>\n"
                "📆 <i>Показываю расписание на завтра:</i>\n\n"
            )
            await send_day_schedule_with_image(
                bot=message.bot,
                chat_id=message.chat.id,
                database=database,
                group_id=group_id,
                group_name=group_name,
                target_date=tomorrow,
                sched=tomorrow_sched,
                message_thread_id=thread_id,
                wait_message=wait_msg,
                notice_prefix=notice,
                show_today_past=bool(lessons),
                is_group=is_group,
                user_subgroup=user_sub,
                subgroup_overrides=user_overrides,
            )
        else:
            await send_day_schedule_with_image(
                bot=message.bot,
                chat_id=message.chat.id,
                database=database,
                group_id=group_id,
                group_name=group_name,
                target_date=today,
                sched=today_sched,
                message_thread_id=thread_id,
                wait_message=wait_msg,
                show_today_past=False,
                is_group=is_group,
                user_subgroup=user_sub,
                subgroup_overrides=user_overrides,
            )

    except Exception as e:
        logger.error("Ошибка получения расписания на сегодня: %s", e, exc_info=True)
        await wait_msg.edit_text("❌ Не удалось загрузить расписание. Пожалуйста, попробуйте позже.")


@router.message(F.text.in_({"📆 На завтра", "📆 Tomorrow"}))
@router.message(Command("tomorrow"))
async def handle_schedule_tomorrow(message: Message, database: Database = default_db):
    """
    Расписание на завтра (сразу с графической картинкой).
    """
    user = await _get_authorized_user(message, database)
    if not user:
        return

    tomorrow = get_current_college_time().date() + timedelta(days=1)
    group_id = user["group_id"]
    group_name = user["group_name"]
    group_url = user["group_url"]
    is_group = message.chat.type in ("group", "supergroup")
    thread_id = getattr(message, "message_thread_id", None)
    await send_chat_action_safe(message.bot, message.chat.id, ChatAction.TYPING, thread_id)

    wait_msg = await message.answer("⏳ <i>Загружаю расписание на завтра...</i>", parse_mode="HTML")

    try:
        sched = await timetable_parser.fetch_day_schedule(
            group_id=group_id, group_url=group_url, target_date=tomorrow
        )
        user_sub = user.get("subgroup", 0) if not is_group else 0
        user_overrides = user.get("subgroup_overrides") if not is_group else None
        await send_day_schedule_with_image(
            bot=message.bot,
            chat_id=message.chat.id,
            database=database,
            group_id=group_id,
            group_name=group_name,
            target_date=tomorrow,
            sched=sched,
            message_thread_id=thread_id,
            wait_message=wait_msg,
            show_today_past=False,
            is_group=is_group,
            user_subgroup=user_sub,
            subgroup_overrides=user_overrides,
        )
    except Exception as e:
        logger.error("Ошибка получения расписания на завтра: %s", e, exc_info=True)
        await wait_msg.edit_text("❌ Не удалось загрузить расписание на завтра. Пожалуйста, попробуйте позже.")


@router.callback_query(ScheduleNavCallback.filter())
async def cb_schedule_nav(
    callback: CallbackQuery, callback_data: ScheduleNavCallback, database: Database = default_db
):
    """
    Обработчик инлайн-кнопок навигации по расписанию:
    - day_to: переключение дней (с сохранением графической карточки)
    - today_past: просмотр прошедшего за сегодня
    - tomorrow: переход к расписанию на завтра
    - image: отдельное получение картинки
    - week: расписание на неделю
    """
    is_group = callback.message.chat.type in ("group", "supergroup")
    if is_group:
        chat_id = callback.message.chat.id
        thread_id = getattr(callback.message, "message_thread_id", None)
        user = await database.get_chat(chat_id, thread_id)
        if not user and thread_id is not None:
            user = await database.get_chat(chat_id, None)
        if not user:
            user = await database.get_user(chat_id)
    else:
        user = await database.get_user(callback.from_user.id)

    if not user or not user.get("group_id"):
        prompt = "Для этой темы/чата еще не выбрана группа: /set_group" if is_group else "Сначала выберите группу: /start"
        await callback.answer(prompt, show_alert=True)
        return

    group_id = user["group_id"]
    group_name = user["group_name"]
    group_url = user["group_url"]
    action = callback_data.action

    try:
        now_dt = get_current_college_time()
        today = now_dt.date()
        thread_id = getattr(callback.message, "message_thread_id", None)
        if action == "image":
            await send_chat_action_safe(callback.bot, callback.message.chat.id, ChatAction.UPLOAD_PHOTO, thread_id)
        else:
            await send_chat_action_safe(callback.bot, callback.message.chat.id, ChatAction.TYPING, thread_id)

        if action in ("day_to", "today", "tomorrow"):
            target_date = today
            if action == "day_to":
                try:
                    target_date = datetime.strptime(callback_data.date_str, "%Y-%m-%d").date()
                except Exception:
                    target_date = today
            elif action == "tomorrow":
                target_date = today + timedelta(days=1)

            sched = await timetable_parser.fetch_day_schedule(
                group_id=group_id, group_url=group_url, target_date=target_date
            )
            thread_id = getattr(callback.message, "message_thread_id", None)
            user_sub = user.get("subgroup", 0) if (user and not is_group) else 0
            user_overrides = user.get("subgroup_overrides") if (user and not is_group) else None
            await send_day_schedule_with_image(
                bot=callback.bot,
                chat_id=callback.message.chat.id,
                database=database,
                group_id=group_id,
                group_name=group_name,
                target_date=target_date,
                sched=sched,
                message_thread_id=thread_id,
                edit_message=callback.message,
                is_group=is_group,
                user_subgroup=user_sub,
                subgroup_overrides=user_overrides,
            )
            await callback.answer()

        elif action == "image":
            await callback.answer("🎨 Отправляю изображение...")
            try:
                target_date = datetime.strptime(callback_data.date_str, "%Y-%m-%d").date()
            except Exception:
                target_date = today

            sched = await timetable_parser.fetch_day_schedule(
                group_id=group_id, group_url=group_url, target_date=target_date
            )
            date_str = target_date.isoformat()
            date_display = target_date.strftime("%d.%m.%Y")
            caption = f"🗓 <b>Расписание группы {group_name}</b> на {date_display}"

            # Проверяем кэш в RAM или SQLite
            cached_fid = _ram_image_cache.get((group_id, date_str)) or await database.get_cached_schedule_image(group_id, date_str)
            if cached_fid:
                try:
                    await callback.message.answer_photo(photo=cached_fid, caption=caption, parse_mode="HTML")
                    return
                except Exception:
                    _ram_image_cache.pop((group_id, date_str), None)

            bot_username = await _get_bot_username(callback.bot)
            img_io = render_schedule_image(sched, group_name, bot_username)
            photo = BufferedInputFile(
                img_io.getvalue(), filename=f"schedule_{group_name}_{date_str}.png"
            )
            sent_p = await callback.message.answer_photo(photo=photo, caption=caption, parse_mode="HTML")
            if sent_p and getattr(sent_p, "photo", None):
                new_fid = sent_p.photo[-1].file_id
                _ram_image_cache[(group_id, date_str)] = new_fid
                await database.save_cached_schedule_image(group_id, date_str, new_fid)

        elif action == "today_past":
            sched = await timetable_parser.fetch_day_schedule(
                group_id=group_id, group_url=group_url, target_date=today
            )
            notice = "📅 <b>Прошедшее расписание за сегодня:</b>\n\n"
            thread_id = getattr(callback.message, "message_thread_id", None)
            await send_day_schedule_with_image(
                bot=callback.bot,
                chat_id=callback.message.chat.id,
                database=database,
                group_id=group_id,
                group_name=group_name,
                target_date=today,
                sched=sched,
                message_thread_id=thread_id,
                edit_message=callback.message,
                notice_prefix=notice,
                show_today_past=False,
                is_group=is_group,
            )
            await callback.answer()

        elif action == "week":
            await callback.answer("Формирую расписание на неделю...")
            try:
                target_date = datetime.strptime(callback_data.date_str, "%Y-%m-%d").date()
            except Exception:
                target_date = today
            week_data = await timetable_parser.fetch_week_schedule(
                group_id=group_id, group_url=group_url, start_date=target_date
            )
            chunks = format_week_schedule_messages(week_data, group_name)
            for chunk in chunks:
                await callback.message.answer(chunk, parse_mode="HTML")

        elif action == "calendar":
            await callback.answer("Генерирую файл календаря...")
            try:
                target_date = datetime.strptime(callback_data.date_str, "%Y-%m-%d").date()
            except Exception:
                target_date = today
            week_data = await timetable_parser.fetch_week_schedule(
                group_id=group_id, group_url=group_url, start_date=target_date
            )
            ics_content = generate_ics_calendar(week_data, group_name)
            filename = f"schedule_{group_name.replace(' ', '_')}.ics"
            doc = BufferedInputFile(ics_content.encode("utf-8"), filename=filename)
            caption = (
                f"📅 <b>Расписание группы {group_name} в формате iCalendar (.ics)</b>\n\n"
                "💡 <i>Откройте этот файл на телефоне (iPhone / Android) или на компьютере, "
                "чтобы добавить расписание в Apple Calendar, Google Calendar или Яндекс Календарь!</i>"
            )
            thread_id = getattr(callback.message, "message_thread_id", None)
            kwargs = {
                "chat_id": callback.message.chat.id,
                "document": doc,
                "caption": caption,
                "parse_mode": "HTML",
            }
            if thread_id is not None:
                kwargs["message_thread_id"] = thread_id
            await callback.bot.send_document(**kwargs)

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
    thread_id = getattr(message, "message_thread_id", None)
    await send_chat_action_safe(message.bot, message.chat.id, ChatAction.TYPING, thread_id)

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


@router.message(F.text.in_({"📍 Где сейчас пара?", "📍 Where is pair now?", "📍 Сейчас", "📍 Now"}))
@router.message(Command("now"))
async def handle_pair_now(message: Message, database: Database = default_db):
    """
    Live-виджет текущей пары:
    - Показывает, идет ли пара сейчас, сколько минут осталось до звонка и какая следующая пара
    - Во время перемены подсказывает, сколько минут отдыхать и в какой кабинет бежать
    - До начала занятий — обратный отсчет до 1-й пары
    - После окончания — поздравление с завершением дня
    """
    user = await _get_authorized_user(message, database)
    if not user:
        return

    now_dt = get_current_college_time()
    today = now_dt.date()
    group_id = user["group_id"]
    group_name = user["group_name"]
    group_url = user["group_url"]
    lang = user.get("language", "ru")
    thread_id = getattr(message, "message_thread_id", None)

    await send_chat_action_safe(message.bot, message.chat.id, ChatAction.TYPING, thread_id)
    wait_msg = await message.answer("🔍 <i>Определяю текущую пару...</i>", parse_mode="HTML")

    try:
        today_sched = await timetable_parser.fetch_day_schedule(
            group_id=group_id, group_url=group_url, target_date=today
        )
        is_group = message.chat.type in ("group", "supergroup")
        user_id = message.from_user.id if not is_group else message.chat.id
        skipped_pairs = await database.get_skipped_pairs(user_id, today.isoformat())
        user_subgroup = user.get("subgroup", 0) if not is_group else 0
        overrides = user.get("subgroup_overrides") if not is_group else None

        filtered_sched = dict(today_sched)
        filtered_sched["lessons"] = filter_lessons_by_subgroup(
            today_sched.get("lessons", []), user_subgroup, overrides
        )
        status_text = get_current_pair_status_info(
            sched=filtered_sched,
            current_dt=now_dt,
            group_name=group_name,
            lang=lang,
            skipped_pairs=skipped_pairs,
        )
        await wait_msg.edit_text(status_text, parse_mode="HTML")
    except Exception as e:
        logger.error("Ошибка определения текущей пары: %s", e, exc_info=True)
        await wait_msg.edit_text("❌ Не удалось получить статус текущей пары. Пожалуйста, попробуйте позже.")


@router.message(F.text.in_({"📅 В календарь", "📅 В календарь (.ics)", "📅 Export to Calendar"}))
@router.message(Command("export_calendar"))
@router.message(Command("calendar"))
async def handle_export_calendar(message: Message, database: Database = default_db):
    """
    Генерирует и отправляет RFC 5545 iCalendar (.ics) файл на текущую неделю.
    """
    user = await _get_authorized_user(message, database)
    if not user:
        return

    today = get_current_college_time().date()
    group_id = user["group_id"]
    group_name = user["group_name"]
    group_url = user["group_url"]
    thread_id = getattr(message, "message_thread_id", None)

    await send_chat_action_safe(message.bot, message.chat.id, ChatAction.TYPING, thread_id)
    wait_msg = await message.answer("⏳ <i>Формирую файл календаря (.ics)...</i>", parse_mode="HTML")

    try:
        week_data = await timetable_parser.fetch_week_schedule(
            group_id=group_id, group_url=group_url, start_date=today
        )
        ics_content = generate_ics_calendar(week_data, group_name)
        filename = f"schedule_{group_name.replace(' ', '_')}.ics"
        doc = BufferedInputFile(ics_content.encode("utf-8"), filename=filename)
        caption = (
            f"📅 <b>Расписание группы {group_name} в формате iCalendar (.ics)</b>\n\n"
            "💡 <i>Откройте этот файл на телефоне (iPhone / Android) или на компьютере, "
            "чтобы добавить расписание в Apple Calendar, Google Calendar или Яндекс Календарь!</i>"
        )
        await wait_msg.delete()
        kwargs = {
            "chat_id": message.chat.id,
            "document": doc,
            "caption": caption,
            "parse_mode": "HTML",
        }
        if thread_id is not None:
            kwargs["message_thread_id"] = thread_id
        await message.bot.send_document(**kwargs)
    except Exception as e:
        logger.error("Ошибка экспорта календаря: %s", e, exc_info=True)
        await wait_msg.edit_text("❌ Не удалось экспортировать календарь. Пожалуйста, попробуйте позже.")



# -------------------------------------------------------------------------
# ПЕРЕКЛЮЧЕНИЕ УВЕДОМЛЕНИЙ
# -------------------------------------------------------------------------

@router.message(F.text.startswith("🔔 Уведомления") | F.text.startswith("🔕 Уведомления") | F.text.in_({"🔔 Настройки уведомлений", "🔔 Notification settings"}))
@router.message(Command("notifications"))
async def handle_toggle_notifications(message: Message, database: Database = default_db):
    """
    Интерактивное меню регулировки времени и статуса автоматических уведомлений.
    """
    is_group = message.chat.type in ("group", "supergroup")
    if is_group:
        chat_id = message.chat.id
        thread_id = getattr(message, "message_thread_id", None)
        chat = await database.get_chat(chat_id, thread_id)
        if not chat:
            await message.answer("Сначала привяжите группу: /set_group или /set_topic")
            return
        evening_time = chat.get("evening_notify_time", "20:00") or "20:00"
        morning_time = chat.get("morning_notify_time", "08:00") or "08:00"
        lead_min = int(chat.get("notify_lead_minutes") or 0)
        enabled = bool(chat.get("notifications_enabled", 1))
        lang = "ru"
    else:
        user_id = message.from_user.id
        user = await database.get_user(user_id)
        if not user:
            await message.answer("Сначала выберите группу: /start")
            return
        evening_time = user.get("evening_notify_time", "20:00") or "20:00"
        morning_time = user.get("morning_notify_time", "08:00") or "08:00"
        lead_min = int(user.get("notify_lead_minutes") or 0)
        enabled = bool(user.get("notifications_enabled", 1))
        lang = user.get("language", "ru")

    kb = get_notification_settings_keyboard(
        evening_time=evening_time,
        morning_time=morning_time,
        lead_min=lead_min,
        enabled=enabled,
        lang=lang,
    )
    text = get_text("notif_settings_title", lang)
    await message.answer(text, reply_markup=kb, parse_mode="HTML")


# -------------------------------------------------------------------------
# ИНСТРУКЦИЯ «ℹ️ БОТ В ГРУППУ» (/group_help)
# -------------------------------------------------------------------------

@router.message(Command("group_help"))
@router.message(F.text == "ℹ️ Бот в группу")
async def cmd_group_help(message: Message):
    """
    Пошаговая инструкция по добавлению бота в беседы и темы форума (Forum Topics).
    """
    bot_username = await _get_bot_username(message.bot)
    kb = get_add_to_group_keyboard(bot_username)
    text = (
        "ℹ️ <b>Как добавить бота в группу или тему (форум):</b>\n\n"
        "1️⃣ <b>Добавить бота в чат/группу</b> и выдать ему права <b>Администратора</b> "
        "(чтобы бот мог видеть команды и отправлять сообщения в чат).\n\n"
        "2️⃣ <b>Перейти в нужный подраздел/тему</b> (например, «Информация» или «Расписание»).\n\n"
        "3️⃣ <b>Отправить команду</b> <code>/set_topic</code> (для конкретной темы форума) или <code>/set_group</code> "
        "(для всей группы/беседы) и выбрать академическую группу колледжа из списка.\n\n"
        "🎉 <b>Готово! Что будет происходить:</b>\n"
        "• Каждый вечер в 20:00 — рассылка расписания на завтра\n"
        "• Каждое утро к 08:00 — утреннее расписание на текущий день\n"
        "• Точные напоминания в момент начала каждой пары\n"
        "• Каждые 15 минут — автоматический мониторинг изменений и замен в расписании!\n\n"
        "👇 <i>Нажмите кнопку ниже, чтобы в один клик добавить бота в группу:</i>"
    )
    await message.answer(text, reply_markup=kb, parse_mode="HTML")


# -------------------------------------------------------------------------
# ПОИСК ПРЕПОДАВАТЕЛЕЙ (/find_teacher)
# -------------------------------------------------------------------------

@router.message(Command("find_teacher"))
@router.message(F.text == "🔍 Поиск преподавателя")
async def cmd_find_teacher(message: Message, state: FSMContext, database: Database = default_db):
    """
    Поиск расписания преподавателей: по ФИО или по предметам группы.
    """
    text_val = message.text or ""
    parts = text_val.split(maxsplit=1)
    query = ""
    if len(parts) > 1 and parts[0].startswith("/find_teacher"):
        query = parts[1].strip()

    if query:
        await _execute_teacher_search(message, query, database)
        return

    kb = get_teacher_search_choice_keyboard()
    await message.answer(
        "🔍 <b>Поиск преподавателей</b>\n\n"
        "Выберите удобный вариант поиска:\n"
        "• <b>🔍 Поиск по ФИО</b> — найти расписание любого преподавателя\n"
        "• <b>📖 Преподаватели моей группы</b> — узнать, кто ведёт конкретный предмет у вашей группы",
        reply_markup=kb,
        parse_mode="HTML"
    )


@router.callback_query(TeacherChoiceCallback.filter())
async def cb_teacher_choice(
    callback: CallbackQuery,
    callback_data: TeacherChoiceCallback,
    state: FSMContext,
    database: Database = default_db,
):
    action = callback_data.action
    if action == "fio":
        await state.set_state(TeacherSearchStates.waiting_for_teacher_name)
        kb = get_teacher_cancel_keyboard()
        await callback.message.edit_text(
            "🔍 <b>Поиск расписания по ФИО преподавателя</b>\n\n"
            "Введите фамилию или ФИО преподавателя (например: <i>Иванов</i>, <i>Смирнова</i> или <i>Петров</i>):",
            reply_markup=kb,
            parse_mode="HTML"
        )
        await callback.answer()

    elif action == "subjects":
        is_group = callback.message.chat.type in ("group", "supergroup")
        if is_group:
            chat_id = callback.message.chat.id
            thread_id = getattr(callback.message, "message_thread_id", None)
            user = await database.get_chat(chat_id, thread_id)
            if not user and thread_id is not None:
                user = await database.get_chat(chat_id, None)
            if not user:
                user = await database.get_user(chat_id)
        else:
            user = await database.get_user(callback.from_user.id)

        if not user or not user.get("group_id"):
            await callback.answer("Сначала выберите группу: /start или /set_group", show_alert=True)
            return

        group_id = user["group_id"]
        group_name = user["group_name"]
        group_url = user.get("group_url", "")

        await callback.answer("Загружаю предметы группы...")
        subjects_dict = await database.get_group_subjects_and_teachers(group_id)

        # Если в кэше пусто, попробуем загрузить расписание текущей недели
        if not subjects_dict and group_url:
            today = get_current_college_time().date()
            await timetable_parser.fetch_week_schedule(group_id, group_url, start_date=today)
            subjects_dict = await database.get_group_subjects_and_teachers(group_id)

        if not subjects_dict:
            await callback.message.edit_text(
                f"⚠️ Для группы <b>{group_name}</b> пока нет загруженных предметов.\n"
                "Попробуйте запросить расписание через команду /week, а затем повторить поиск.",
                reply_markup=get_teacher_search_choice_keyboard(),
                parse_mode="HTML"
            )
            return

        subj_list = sorted(list(subjects_dict.keys()))
        kb = get_group_subjects_inline_keyboard(subj_list)
        await callback.message.edit_text(
            f"📖 <b>Преподаватели группы {group_name}</b>\n\n"
            "Нажмите на интересующий <b>предмет</b>, чтобы узнать преподавателя, аудиторию и время пар:",
            reply_markup=kb,
            parse_mode="HTML"
        )

    elif action == "menu":
        await callback.message.edit_text(
            "🔍 <b>Поиск преподавателей</b>\n\n"
            "Выберите удобный вариант поиска:\n"
            "• <b>🔍 Поиск по ФИО</b> — найти расписание любого преподавателя\n"
            "• <b>📖 Преподаватели моей группы</b> — узнать, кто ведёт конкретный предмет у вашей группы",
            reply_markup=get_teacher_search_choice_keyboard(),
            parse_mode="HTML"
        )
        await callback.answer()


@router.callback_query(SubjectCallback.filter())
async def cb_subject_detail(
    callback: CallbackQuery,
    callback_data: SubjectCallback,
    database: Database = default_db,
):
    is_group = callback.message.chat.type in ("group", "supergroup")
    if is_group:
        chat_id = callback.message.chat.id
        thread_id = getattr(callback.message, "message_thread_id", None)
        user = await database.get_chat(chat_id, thread_id)
        if not user and thread_id is not None:
            user = await database.get_chat(chat_id, None)
        if not user:
            user = await database.get_user(chat_id)
    else:
        user = await database.get_user(callback.from_user.id)

    if not user or not user.get("group_id"):
        await callback.answer("Сначала выберите группу: /start или /set_group", show_alert=True)
        return

    group_id = user["group_id"]
    group_name = user["group_name"]
    subjects_dict = await database.get_group_subjects_and_teachers(group_id)
    subj_list = sorted(list(subjects_dict.keys()))

    if callback_data.idx >= len(subj_list):
        await callback.answer("Предмет не найден.", show_alert=True)
        return

    subj_name = subj_list[callback_data.idx]
    info = subjects_dict[subj_name]

    teachers = info.get("teachers", [])
    teachers_str = "\n".join([f"• <b>{t}</b>" for t in teachers]) if teachers else "• <i>Не указан в расписании</i>"

    rooms = info.get("rooms", [])
    rooms_str = ", ".join([f"Ауд. {r}" for r in rooms]) if rooms else "Не указана"

    lessons = info.get("lessons", [])
    lessons_str = "\n".join(lessons[:4]) if lessons else "• <i>На ближайшие дни пар нет</i>"

    text = (
        f"📖 <b>Предмет:</b> <code>{subj_name}</code>\n"
        f"👥 <b>Группа:</b> <code>{group_name}</code>\n"
        "────────────────────\n"
        f"👤 <b>Преподаватель(и):</b>\n{teachers_str}\n\n"
        f"🚪 <b>Аудитория(и):</b> {rooms_str}\n\n"
        f"🗓 <b>Ближайшие пары в расписании:</b>\n{lessons_str}"
    )

    from aiogram.types import InlineKeyboardButton
    from aiogram.utils.keyboard import InlineKeyboardBuilder

    builder = InlineKeyboardBuilder()
    builder.row(
        InlineKeyboardButton(
            text="⬅️ Назад к списку предметов",
            callback_data=TeacherChoiceCallback(action="subjects").pack()
        )
    )
    builder.row(
        InlineKeyboardButton(
            text="🏠 В главное меню",
            callback_data=NavigationCallback(to="main_menu").pack()
        )
    )

    await callback.message.edit_text(text, reply_markup=builder.as_markup(), parse_mode="HTML")
    await callback.answer()


@router.message(TeacherSearchStates.waiting_for_teacher_name)
async def process_teacher_search_input(message: Message, state: FSMContext, database: Database = default_db):
    """
    Обработка введенного пользователем запроса поиска преподавателя.
    """
    await state.clear()
    query = (message.text or "").strip()
    if not query:
        await message.answer("⚠️ Введен пустой запрос. Попробуйте снова командой /find_teacher.")
        return

    await _execute_teacher_search(message, query, database)


async def _execute_teacher_search(message: Message, query: str, database: Database) -> None:
    """
    Выполняет сканирование расписания в timetable_cache и выводит список пар преподавателя
    с объединением подгрупп, исключая дублирование одинаковых занятий.
    """
    thread_id = getattr(message, "message_thread_id", None)
    await send_chat_action_safe(message.bot, message.chat.id, ChatAction.TYPING, thread_id)
    wait_msg = await message.answer(f"🔍 <i>Ищу расписание для «{query}» по всем группам...</i>", parse_mode="HTML")

    try:
        results = await database.find_teacher_schedule(query, days_ahead=7)
    except Exception as e:
        logger.error("Ошибка поиска преподавателя: %s", e, exc_info=True)
        await wait_msg.edit_text("❌ Произошла ошибка при поиске расписания. Пожалуйста, попробуйте позже.")
        return

    if not results:
        await wait_msg.edit_text(
            f"❌ Пары для преподавателя «<b>{query}</b>» не найдены в сохраненном расписании на ближайшие дни.\n\n"
            "💡 <i>Подсказка: проверьте правильность написания фамилии или укажите только часть фамилии.</i>",
            parse_mode="HTML"
        )
        return

    by_date: dict[str, list[dict]] = {}
    found_teacher_name = results[0]["teacher"]
    for item in results:
        by_date.setdefault(item["date"], []).append(item)

    lines = [
        f"🔍 <b>Расписание преподавателя:</b> <code>{found_teacher_name}</code>",
        f"📌 Найдено занятий: <b>{len(results)}</b> на ближайшую неделю\n"
    ]

    for date_val, day_lessons in by_date.items():
        day_name = day_lessons[0].get("day_name", "")
        formatted_date = date_val
        try:
            dt = datetime.strptime(date_val, "%Y-%m-%d")
            formatted_date = f"{dt.day} {MONTH_NAMES_GENITIVE[dt.month]}"
        except Exception:
            pass

        lines.append(f"📅 <b>{day_name}</b>, {formatted_date}:")

        # Группируем уроки одного времени и предмета, чтобы объединить подгруппы
        grouped_time: dict[tuple, list[dict]] = {}
        for l in day_lessons:
            key = (l.get("pair_number", 1), l.get("time", ""), l.get("subject", "Занятие"), l.get("group_name", ""))
            grouped_time.setdefault(key, []).append(l)

        for (pair_num, time_str, subject, grp_name), items in grouped_time.items():
            t_str = f" ({time_str})" if time_str else ""
            lines.append(f"🔹 <b>{pair_num} пара</b>{t_str} — {subject}")
            if len(items) > 1:
                rooms_parts = []
                for it in items:
                    sub = it.get("subgroup", 0)
                    sub_str = f" ({sub} подгр.)" if sub else ""
                    r = it.get("room") or "Ауд."
                    rooms_parts.append(f"{r}{sub_str}")
                lines.append(f"   👥 Группа: <code>{grp_name}</code> | 🚪 Ауд: {', '.join(rooms_parts)}")
            else:
                it = items[0]
                room = it.get("room") or "не указана"
                subgroup = it.get("subgroup", 0)
                subgroup_str = f" | 👥 {subgroup} подгр." if subgroup else ""
                lines.append(f"   👥 Группа: <code>{grp_name}</code> | 🚪 Ауд: {room}{subgroup_str}")
        lines.append("")

    response_text = "\n".join(lines).strip()
    if len(response_text) > 4000:
        response_text = response_text[:3950] + "\n\n<i>...часть расписания скрыта из-за ограничения длины</i>"

    await wait_msg.edit_text(response_text, parse_mode="HTML")


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
            "• При первом запуске (/start) выберите язык, специальность и группу.\n"
            "• Сменить группу можно в любой момент кнопкой «⚙️ Сменить группу» или командой /change_group.\n\n"
            "2️⃣ <b>Просмотр расписания:</b>\n"
            "• Кнопка «📅 На сегодня» (/today) — расписание на текущий день.\n"
            "• <i>Умный режим:</i> если пары сегодня уже завершились, бот автоматически покажет расписание на завтра и предложит кнопку «⏪ Показать прошедшее за сегодня».\n"
            "• Кнопка «📆 На завтра» (/tomorrow) — расписание на следующий день.\n"
            "• Кнопка «🗓 На неделю» (/week) — полное расписание с понедельника по субботу.\n"
            "• Кнопка «🔍 Поиск преподавателя» (/find_teacher) — поиск расписания преподавателя.\n\n"
            "3️⃣ <b>Уведомления:</b>\n"
            "• <b>08:00</b> каждое утро — свежее расписание на день.\n"
            "• <b>20:00</b> каждый вечер — расписание на завтрашний день.\n"
            "• Напоминания перед парами — за 5, 10, 15, 20, 30 или 45 минут до звонка!\n"
            "• Каждые 15 минут — мониторинг замен в расписании.\n\n"
            "4️⃣ <b>Домашние задания и Староста:</b>\n"
            "• Кнопка «📚 ДЗ 📚» (/hw) — просмотр актуальных заданий группы.\n"
            "• Кнопка «🙋‍♂️ Староста» (/starosta) — назначение старосты группы.\n"
            "• Староста может добавлять ДЗ по предметам и датам сдачи.\n"
            "• Бот <b>автоматически транслирует</b> опубликованное ДЗ всем студентам группы и в чат группы, а также прикрепляет к вечерней рассылке на завтра!\n\n"
            "👨‍💻 <i>Разработчик бота: @yapsychokid</i>"
        )
    elif section == "group":
        return (
            "👥 <b>Инструкция: Как добавить бота в группу (беседу) или тему форума:</b>\n\n"
            "Вы можете добавить бота в общую беседу вашей студенческой группы или в отдельный топик форума!\n\n"
            "<b>Шаг 1: Добавление бота</b>\n"
            "• Откройте чат вашей группы в Telegram.\n"
            "• Добавьте бота в чат и назначьте его <b>администратором</b>.\n\n"
            "<b>Шаг 2: Привязка учебной группы</b>\n"
            "• В обычной беседе: отправьте команду /set_group\n"
            "• В теме (подразделе/топике форума): перейдите в тему и отправьте команду /set_topic\n"
            "• В появившемся меню выберите вашу специальность и группу колледжа.\n\n"
            "🎉 <b>Готово! Что будет дальше:</b>\n"
            "• Бот будет присылать вечернюю рассылку в 20:00 и утреннюю в 08:00 прямо в чат или топик!\n"
            "• Бот будет присылать предупреждения в случае замены расписания!\n"
            "• <b>Трансляция ДЗ:</b> когда староста группы пишет ДЗ в боте, оно моментально отправляется в этот чат!\n"
            "• Любой студент в группе может написать /today, /tomorrow, /week или /hw.\n\n"
            "👨‍💻 <i>Разработчик бота: @yapsychokid</i>"
        )
    elif section == "commands":
        return (
            "📋 <b>Полный список команд бота:</b>\n\n"
            "• /start — Главное меню и запуск\n"
            "• /today — Расписание на сегодня\n"
            "• /tomorrow — Расписание на завтра\n"
            "• /week — Расписание на всю учебную неделю\n"
            "• /hw — Домашние задания группы\n"
            "• /starosta — Меню старосты и управление ДЗ\n"
            "• /lang — Переключение языка (RU / EN)\n"
            "• /find_teacher — Поиск расписания преподавателя\n"
            "• /group_help — Инструкция по добавлению в беседу/топик\n"
            "• /set_topic — Настройка темы (для топиков супергруппы)\n"
            "• /set_group — Настроить/сменить группу\n"
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
            "присылать утренние и вечерние рассылки, транслировать домашние задания от старосты, "
            "а также работать в учебных беседах!\n\n"
            "🔹 <b>В личных сообщениях:</b>\n"
            "Используйте удобные кнопки главного меню под клавиатурой для быстрого просмотра на сегодня, "
            "завтра и неделю. Верхняя кнопка «📚 ДЗ 📚» покажет актуальные задания.\n\n"
            "🔹 <b>Староста группы:</b>\n"
            "Нажмите «🙋‍♂️ Староста», чтобы стать старостой и публиковать домашние задания. "
            "Бот автоматически разошлет их всем однокурсникам и в чат группы!\n\n"
            "🔹 <b>В группах и беседах:</b>\n"
            "Добавьте бота в чат своей учебной группы, напишите /set_group — и бот будет присылать "
            "расписание и домашние задания прямо в беседу!\n\n"
            "👇 <i>Выберите раздел ниже для подробной инструкции:</i>"
        )


@router.message(Command("help"))
@router.message(Command("guide"))
@router.message(F.text.in_({"📖 Инструкция", "📖 Guide"}))
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
# СТАРОСТА ГРУППЫ (starostas)
# -------------------------------------------------------------------------

@router.message(Command("starosta"))
@router.message(F.text.in_({"🙋‍♂️ Староста 🙋‍♂️", "🙋‍♂️ Староста", "🙋‍♂️ Starosta 🙋‍♂️", "🙋‍♂️ Starosta"}))
async def cmd_starosta(message: Message, database: Database = default_db):
    """
    Меню управления старостой группы и публикации домашних заданий.
    """
    user_id = message.from_user.id
    user = await database.get_user(user_id)
    lang = user.get("language", "ru") if user else "ru"
    group_id = user.get("group_id") if user else None
    group_name = user.get("group_name", "") if user else ""

    if not group_id:
        await message.answer(
            "⚠️ Сначала выберите вашу группу с помощью кнопки «⚙️ Сменить группу»!"
            if lang == "ru"
            else "⚠️ Please select your group first using «⚙️ Change group»!"
        )
        return

    starosta = await database.get_group_starosta(group_id)
    deputy = await database.get_group_deputy(group_id)
    is_current = (starosta["user_id"] == user_id) if starosta else False
    is_deputy = (deputy["user_id"] == user_id) if deputy else False

    if not starosta:
        pending = await database.get_pending_starosta_request(group_id, user_id, "claim")
        kb = get_starosta_inline_keyboard(is_current_user=False, has_starosta=False, lang=lang)
        if pending:
            text = (
                f"У группы <b>{group_name}</b> еще нет подтвержденного старосты.\n\n"
                f"⏳ <i>Ваша заявка на статус старосты передана администратору бота и ожидает рассмотрения.</i>"
                if lang == "ru"
                else f"Group <b>{group_name}</b> has no starosta yet.\n\n"
                f"⏳ <i>Your starosta application is pending bot administrator approval.</i>"
            )
        else:
            text = (
                f"У вашей группы <b>{group_name}</b> нет старосты.\n\n"
                "Вы можете подать заявку на статус старосты. "
                "После подтверждения администратором бота вы сможете публиковать домашние задания."
                if lang == "ru"
                else f"Group <b>{group_name}</b> has no starosta.\n\n"
                "You can apply to become starosta. "
                "Once approved by the bot administrator, you will be able to manage homework."
            )
        await message.answer(text, reply_markup=kb, parse_mode="HTML")
    else:
        kb = get_starosta_inline_keyboard(
            is_current_user=is_current,
            has_starosta=True,
            has_deputy=bool(deputy),
            is_deputy=is_deputy,
            lang=lang,
        )
        starosta_name = starosta.get("full_name") or starosta.get("username") or "Студент"
        uname = f"@{starosta['username']}" if starosta.get("username") else "без @username"
        appointed_date = str(starosta.get("appointed_at", ""))[:10]

        dep_info = ""
        if deputy:
            d_name = deputy.get("full_name") or "Студент"
            d_uname = f"@{deputy['username']}" if deputy.get("username") else f"ID {deputy['user_id']}"
            dep_info = f"\n👥 <b>Заместитель старосты:</b> {d_name} ({d_uname})"
        else:
            dep_info = "\n👥 <b>Заместитель старосты:</b> Не назначен"

        if is_current:
            text = (
                f"👑 <b>Вы — староста группы {group_name}!</b>\n"
                f"{dep_info}\n\n"
                "Вы и ваш заместитель можете добавлять домашние задания и привязывать чат группы.\n\n"
                "<i>Примечание: сложить полномочия или сменить старосту можно только через администратора бота. Заместителя вы можете назначить или снять в любой момент.</i>\n\n"
                "Используйте кнопки ниже для управления:"
                if lang == "ru"
                else f"👑 <b>You are the starosta of group {group_name}!</b>\n"
                f"{dep_info}\n\n"
                "You can add homework assignments and manage group chat.\n\n"
                "Use the buttons below to manage:"
            )
        elif is_deputy:
            text = (
                f"🎖 <b>Вы — заместитель старосты группы {group_name}!</b>\n\n"
                f"👑 <b>Староста:</b> {starosta_name} ({uname})\n\n"
                "Вам доступны функции публикации и удаления домашних заданий (📚 ДЗ), а также настройка ссылки на беседу группы (💬 Чат группы).\n\n"
                "Используйте кнопки ниже:"
                if lang == "ru"
                else f"🎖 <b>You are the deputy starosta of group {group_name}!</b>\n\n"
                f"👑 <b>Starosta:</b> {starosta_name} ({uname})\n\n"
                "You can manage homework and group chat link."
            )
        else:
            text = (
                f"👑 <b>Староста группы {group_name}:</b>\n"
                f"👤 {starosta_name} ({uname})\n"
                f"{dep_info}\n"
                f"📅 Назначен(а): {appointed_date}\n\n"
                "<i>Староста и заместитель публикуют домашние задания, которые рассылаются группе и прикрепляются к расписанию.</i>"
                if lang == "ru"
                else f"👑 <b>Starosta of group {group_name}:</b>\n"
                f"👤 {starosta_name} ({uname})\n"
                f"{dep_info}\n"
                f"📅 Appointed: {appointed_date}\n\n"
                "<i>The starosta and deputy manage homework assignments.</i>"
            )
        await message.answer(text, reply_markup=kb, parse_mode="HTML")


@router.callback_query(StarostaCallback.filter(F.action == "claim"))
async def cb_claim_starosta(
    callback: CallbackQuery, database: Database = default_db
):
    """
    Подача заявки на старосту (отправляется администраторам бота на подтверждение).
    """
    user_id = callback.from_user.id
    user = await database.get_user(user_id)
    lang = user.get("language", "ru") if user else "ru"
    group_id = user.get("group_id") if user else None
    group_name = user.get("group_name", "") if user else ""

    if not group_id:
        await callback.answer("Сначала выберите группу!", show_alert=True)
        return

    existing = await database.get_group_starosta(group_id)
    if existing:
        await callback.answer("У группы уже есть староста!", show_alert=True)
        return

    # Если действие выполняет сам бот-админ — мгновенное назначение
    if config.is_admin(user_id):
        username = callback.from_user.username
        full_name = callback.from_user.full_name
        await database.set_group_starosta(group_id, user_id, username, full_name)
        kb = get_starosta_inline_keyboard(is_current_user=True, has_starosta=True, lang=lang)
        success_text = (
            f"👑 <b>Вы (как администратор) назначены старостой группы {group_name}.</b>\n\n"
            "Теперь вы можете публиковать домашние задания через меню ДЗ."
        )
        await callback.message.edit_text(success_text, reply_markup=kb, parse_mode="HTML")
        await callback.answer("Назначен старостой!")
        return

    # Проверяем, нет ли уже ожидающей заявки от этого студента
    pending = await database.get_pending_starosta_request(group_id, user_id, "claim")
    if pending:
        await callback.answer("Ваша заявка уже на рассмотрении у администратора!", show_alert=True)
        return

    # Создаем заявку в базе данных
    username = callback.from_user.username
    full_name = callback.from_user.full_name
    req_id = await database.create_starosta_request(
        group_id=group_id,
        user_id=user_id,
        username=username,
        full_name=full_name,
        request_type="claim",
    )

    # Оповещаем администраторов бота с инлайн-кнопками Одобрить / Отклонить
    cand_name = full_name or "Студент"
    cand_uname = f"@{username}" if username else "нет username"
    admin_kb = get_admin_starosta_decision_keyboard(req_id=req_id, group_id=group_id, candidate_id=user_id)
    admin_text = (
        f"📬 <b>Новая заявка на старосту группы!</b>\n\n"
        f"👥 Группа: <b>{group_name}</b> (ID: <code>{group_id}</code>)\n"
        f"👤 Кандидат: <b>{cand_name}</b> ({cand_uname})\n"
        f"🆔 Telegram ID: <code>{user_id}</code>\n"
        f"⏰ Время подачи: {get_current_college_time().strftime('%d.%m.%Y %H:%M')}\n\n"
        "Подтвердить назначение старосты?"
    )

    for admin_id in config.ADMIN_IDS:
        try:
            await callback.bot.send_message(
                chat_id=admin_id,
                text=admin_text,
                reply_markup=admin_kb,
                parse_mode="HTML"
            )
        except Exception as ex:
            logger.warning("Не удалось отправить заявку админу %d: %s", admin_id, ex)

    kb = get_starosta_inline_keyboard(is_current_user=False, has_starosta=False, lang=lang)
    sent_text = (
        f"📨 <b>Заявка отправлена администратору!</b>\n\n"
        f"Ваша заявка на пост старосты группы <b>{group_name}</b> передана главному администратору бота.\n\n"
        "Как только администратор подтвердит назначение, вы получите уведомление и сможете добавлять домашние задания."
        if lang == "ru"
        else f"📨 <b>Application sent!</b>\n\n"
        f"Your starosta application for group <b>{group_name}</b> has been sent to the administrator.\n\n"
        "You will receive a notification once approved."
    )
    await callback.message.edit_text(sent_text, reply_markup=kb, parse_mode="HTML")
    await callback.answer("Заявка передана администратору бота!", show_alert=True)


@router.callback_query(StarostaCallback.filter(F.action == "resign"))
async def cb_resign_starosta(
    callback: CallbackQuery, database: Database = default_db
):
    """
    Сложение полномочий старосты группы: возможно только через подтверждение администратором.
    """
    user_id = callback.from_user.id
    user = await database.get_user(user_id)
    lang = user.get("language", "ru") if user else "ru"
    group_id = user.get("group_id") if user else None
    group_name = user.get("group_name", "") if user else ""

    if not group_id:
        await callback.answer("Сначала выберите группу!", show_alert=True)
        return

    starosta = await database.get_group_starosta(group_id)
    if not starosta:
        await callback.answer("У группы нет старосты.", show_alert=True)
        return

    # Если действие выполняет сам бот-администратор — мгновенно снимаем
    if config.is_admin(user_id):
        await database.remove_group_starosta(group_id)
        text = "Вы успешно сняли старосту группы." if lang == "ru" else "You have removed group starosta."
        kb = get_starosta_inline_keyboard(is_current_user=False, has_starosta=False, lang=lang)
        await callback.message.edit_text(text, reply_markup=kb, parse_mode="HTML")
        await callback.answer("Староста снят.")
        return

    if starosta["user_id"] != user_id:
        await callback.answer("Только староста группы или администратор бота может запросить снятие!", show_alert=True)
        return

    # Запрос на сложение полномочий отправляется главному администратору
    rem_kb = get_admin_starosta_remove_keyboard(group_id=group_id, starosta_id=user_id)
    st_uname = f"@{callback.from_user.username}" if callback.from_user.username else "нет username"
    st_name = callback.from_user.full_name or "Староста"

    for admin_id in config.ADMIN_IDS:
        try:
            await callback.bot.send_message(
                chat_id=admin_id,
                text=(
                    f"⚠️ <b>Запрос на сложение полномочий старосты!</b>\n\n"
                    f"👥 Группа: <b>{group_name}</b> (ID: <code>{group_id}</code>)\n"
                    f"👤 Староста: <b>{st_name}</b> ({st_uname})\n"
                    f"🆔 Telegram ID: <code>{user_id}</code>\n\n"
                    "Снять старосту с должности?"
                ),
                reply_markup=rem_kb,
                parse_mode="HTML"
            )
        except Exception as ex:
            logger.warning("Не удалось отправить запрос на снятие админу %d: %s", admin_id, ex)

    text = (
        f"📨 <b>Запрос отправлен!</b>\n\n"
        f"Запрос на сложение полномочий старосты группы <b>{group_name}</b> направлен главному администратору бота. "
        "Снять старосту может только администратор."
        if lang == "ru"
        else f"📨 <b>Request sent!</b>\n\n"
        f"Resignation request for starosta of group <b>{group_name}</b> forwarded to administrator. "
        "Only the administrator can revoke starosta status."
    )
    deputy = await database.get_group_deputy(group_id)
    kb = get_starosta_inline_keyboard(is_current_user=True, has_starosta=True, has_deputy=bool(deputy), is_deputy=False, lang=lang)
    await callback.message.edit_text(text, reply_markup=kb, parse_mode="HTML")
    await callback.answer("Запрос передан администратору!", show_alert=True)


@router.callback_query(StarostaCallback.filter(F.action == "add_deputy"))
async def cb_starosta_add_deputy(
    callback: CallbackQuery, state: FSMContext, database: Database = default_db
):
    """Староста запускает процесс назначения заместителя."""
    user_id = callback.from_user.id
    user = await database.get_user(user_id)
    lang = user.get("language", "ru") if user else "ru"
    group_id = user.get("group_id") if user else None

    if not group_id:
        await callback.answer("Сначала выберите группу!", show_alert=True)
        return

    starosta = await database.get_group_starosta(group_id)
    if not starosta or (starosta["user_id"] != user_id and not config.is_admin(user_id)):
        await callback.answer("Только староста группы может назначить заместителя!", show_alert=True)
        return

    deputy = await database.get_group_deputy(group_id)
    if deputy:
        await callback.answer("У группы уже назначен заместитель! Сначала снимите текущего.", show_alert=True)
        return

    await state.set_state(StarostaDeputyStates.waiting_for_deputy_input)
    prompt = (
        "👤 <b>Назначение заместителя старосты</b>\n\n"
        "Отправьте <code>@username</code> студента вашей группы или перешлите его сообщение / отправьте числовой Telegram ID:\n\n"
        "<i>Заместитель сможет публиковать и удалять домашние задания, а также настраивать ссылку на беседу группы. Можно назначить только одного зама.</i>"
        if lang == "ru"
        else (
            "👤 <b>Appoint Deputy Starosta</b>\n\n"
            "Send the <code>@username</code> or Telegram ID of a student from your group:\n\n"
            "<i>The deputy can manage homework and group chat link. Only 1 deputy is allowed.</i>"
        )
    )
    await callback.message.answer(prompt, parse_mode="HTML")
    await callback.answer()


@router.callback_query(StarostaCallback.filter(F.action == "remove_deputy"))
async def cb_starosta_remove_deputy(
    callback: CallbackQuery, database: Database = default_db
):
    """Староста снимает заместителя."""
    user_id = callback.from_user.id
    user = await database.get_user(user_id)
    lang = user.get("language", "ru") if user else "ru"
    group_id = user.get("group_id") if user else None

    if not group_id:
        await callback.answer("Сначала выберите группу!", show_alert=True)
        return

    starosta = await database.get_group_starosta(group_id)
    if not starosta or (starosta["user_id"] != user_id and not config.is_admin(user_id)):
        await callback.answer("Только староста группы может снять заместителя!", show_alert=True)
        return

    deputy = await database.get_group_deputy(group_id)
    if not deputy:
        await callback.answer("У группы нет заместителя.", show_alert=True)
        return

    deputy_id = deputy["user_id"]
    await database.remove_group_deputy(group_id)

    await callback.answer("Заместитель старосты снят с должности.")
    kb = get_starosta_inline_keyboard(is_current_user=True, has_starosta=True, has_deputy=False, is_deputy=False, lang=lang)
    group_name = user.get("group_name", "")
    await callback.message.edit_text(
        f"👑 <b>Вы — староста группы {group_name}!</b>\n\n"
        "👤 <b>Заместитель старосты:</b> Не назначен\n\n"
        "Вы можете добавить нового заместителя кнопкой ниже.",
        reply_markup=kb,
        parse_mode="HTML"
    )

    try:
        await callback.bot.send_message(
            chat_id=deputy_id,
            text=f"ℹ️ Вы были сняты с должности заместителя старосты группы <b>{group_name}</b>.",
            parse_mode="HTML"
        )
    except Exception:
        pass


@router.message(StarostaDeputyStates.waiting_for_deputy_input)
async def process_deputy_input(message: Message, state: FSMContext, database: Database = default_db):
    """Обработка ввода username или ID заместителя."""
    await state.clear()
    sender_id = message.from_user.id
    sender = await database.get_user(sender_id)
    group_id = sender.get("group_id") if sender else None
    group_name = sender.get("group_name", "")
    lang = sender.get("language", "ru") if sender else "ru"

    if not group_id:
        await message.answer("⚠️ Сначала выберите вашу группу!")
        return

    starosta = await database.get_group_starosta(group_id)
    if not starosta or (starosta["user_id"] != sender_id and not config.is_admin(sender_id)):
        await message.answer("⚠️ Только староста группы может назначать заместителя.")
        return

    text = (message.text or "").strip()
    target_user = None

    if text.isdigit():
        target_user = await database.get_user(int(text))
    else:
        target_user = await database.get_user_by_username(text)

    if not target_user:
        await message.answer(
            f"❌ Пользователь «<b>{text}</b>» не найден в базе бота.\n\n"
            "💡 Попросите студента сначала запустить этого бота (/start), чтобы бот мог его идентифицировать.",
            parse_mode="HTML"
        )
        return

    cand_id = target_user["user_id"]
    if cand_id == sender_id:
        await message.answer("⚠️ Вы не можете назначить заместителем самого себя!")
        return

    cand_group_id = target_user.get("group_id")
    if cand_group_id != group_id:
        await message.answer(
            f"⚠️ Этот студент числится в другой группе (<b>{target_user.get('group_name', 'не выбрана')}</b>).\n"
            "Заместителем можно назначить только студента из вашей группы!",
            parse_mode="HTML"
        )
        return

    cand_uname = target_user.get("username")
    cand_name = target_user.get("first_name") or "Студент"

    await database.set_group_deputy(group_id, cand_id, cand_uname, cand_name)

    kb = get_starosta_inline_keyboard(is_current_user=True, has_starosta=True, has_deputy=True, is_deputy=False, lang=lang)
    cand_display = f"@{cand_uname}" if cand_uname else f"ID {cand_id}"
    await message.answer(
        f"✅ <b>Заместитель успешно назначен!</b>\n\n"
        f"👤 <b>{cand_name}</b> ({cand_display})\n"
        f"👥 Группа: <b>{group_name}</b>\n\n"
        "Теперь ваш заместитель может публиковать и удалять домашние задания, а также настраивать ссылку на беседу группы.",
        reply_markup=kb,
        parse_mode="HTML"
    )

    try:
        await message.bot.send_message(
            chat_id=cand_id,
            text=(
                f"🎉 <b>Староста группы {group_name} назначил вас своим заместителем!</b>\n\n"
                "Вам открыт доступ к добавлению и удалению домашних заданий (кнопка «📚 ДЗ») "
                "и обновлению ссылки на беседу вашей группы (кнопка «💬 Чат группы»)."
            ),
            parse_mode="HTML"
        )
    except Exception as ex:
        logger.warning("Не удалось уведомить назначенного зама %d: %s", cand_id, ex)


@router.callback_query(AdminStarostaApproveCallback.filter())
async def cb_admin_starosta_decision(
    callback: CallbackQuery,
    callback_data: AdminStarostaApproveCallback,
    database: Database = default_db,
):
    """
    Обработка решений администратора по заявкам старост: одобрение, отклонение, снятие.
    """
    admin_id = callback.from_user.id
    if not config.is_admin(admin_id):
        await callback.answer("⛔ Только администратор бота может принимать решения по старостам!", show_alert=True)
        return

    action = callback_data.action
    req_id = callback_data.req_id
    group_id = callback_data.group_id
    candidate_id = callback_data.candidate_id

    group = await database.get_group_by_id(group_id)
    group_name = group["group_name"] if group else group_id

    if action == "approve":
        req = await database.get_starosta_request_by_id(req_id) if req_id > 0 else None
        if req and req.get("status") != "pending":
            await callback.answer(f"Заявка уже обработана (статус: {req.get('status')})", show_alert=True)
            return

        cand_user = await database.get_user(candidate_id)
        username = cand_user.get("username") if cand_user else (req.get("username") if req else None)
        full_name = cand_user.get("first_name") if cand_user else (req.get("full_name") if req else None)

        await database.set_group_starosta(group_id, candidate_id, username, full_name)
        if req_id > 0:
            await database.update_starosta_request_status(req_id, "approved")

        cand_title = full_name or (f"@{username}" if username else f"ID {candidate_id}")
        await callback.message.edit_text(
            f"✅ <b>Заявка ОДОБРЕНА администратором!</b>\n\n"
            f"👥 Группа: <b>{group_name}</b>\n"
            f"👑 Староста: <b>{cand_title}</b> (ID: <code>{candidate_id}</code>)\n"
            f"📅 Назначен: {get_current_college_time().strftime('%d.%m.%Y %H:%M')}",
            parse_mode="HTML"
        )
        await callback.answer("Староста назначен!")

        # Уведомляем кандидата
        cand_lang = cand_user.get("language", "ru") if cand_user else "ru"
        try:
            await callback.bot.send_message(
                chat_id=candidate_id,
                text=get_text("starosta_approved_notify", lang=cand_lang, group_name=group_name),
                parse_mode="HTML"
            )
        except Exception as ex:
            logger.warning("Не удалось уведомить кандидата %d: %s", candidate_id, ex)

    elif action == "reject":
        if req_id > 0:
            await database.update_starosta_request_status(req_id, "rejected")

        await callback.message.edit_text(
            f"❌ <b>Заявка ОТКЛОНЕНА администратором.</b>\n\n"
            f"👥 Группа: <b>{group_name}</b>\n"
            f"👤 Кандидат ID: <code>{candidate_id}</code>",
            parse_mode="HTML"
        )
        await callback.answer("Заявка отклонена.")

        cand_user = await database.get_user(candidate_id)
        cand_lang = cand_user.get("language", "ru") if cand_user else "ru"
        try:
            await callback.bot.send_message(
                chat_id=candidate_id,
                text=get_text("starosta_rejected_notify", lang=cand_lang, group_name=group_name),
                parse_mode="HTML"
            )
        except Exception as ex:
            logger.warning("Не удалось уведомить кандидата %d: %s", candidate_id, ex)

    elif action == "rem_confirm":
        await database.remove_group_starosta(group_id)
        await callback.message.edit_text(
            f"🗑 <b>СТАРОСТА СНЯТ администратором.</b>\n\n"
            f"👥 Группа: <b>{group_name}</b>",
            parse_mode="HTML"
        )
        await callback.answer("Староста снят!")

        if candidate_id:
            cand_user = await database.get_user(candidate_id)
            cand_lang = cand_user.get("language", "ru") if cand_user else "ru"
            try:
                await callback.bot.send_message(
                    chat_id=candidate_id,
                    text=(
                        f"ℹ️ Ваш статус старосты группы <b>{group_name}</b> был снят администратором бота."
                        if cand_lang == "ru"
                        else f"ℹ️ Your starosta status for group <b>{group_name}</b> was revoked by the bot administrator."
                    ),
                    parse_mode="HTML"
                )
            except Exception:
                pass

    elif action == "rem_cancel":
        await callback.message.edit_text(
            f"✋ <b>Снятие старосты отменено.</b>\n\n"
            f"👥 Группа: <b>{group_name}</b>",
            parse_mode="HTML"
        )
        await callback.answer("Отменено.")


@router.message(Command("set_starosta"))
async def cmd_admin_set_starosta(message: Message, database: Database = default_db):
    """
    Команда администратора для прямого назначения старосты или доверенного лица:
    /set_starosta <group_id или название> <user_id или @username>
    """
    admin_id = message.from_user.id
    if not config.is_admin(admin_id):
        await message.answer("⛔ Доступно только главному администратору бота.")
        return

    args = (message.text or "").split()[1:]
    if len(args) < 2:
        await message.answer(
            "ℹ️ <b>Использование:</b> <code>/set_starosta &lt;group_id/название&gt; &lt;user_id/@username&gt;</code>\n\n"
            "Пример: <code>/set_starosta ИС-21 870396858</code>\n"
            "Или: <code>/set_starosta 6g5eqnp6 @username</code>",
            parse_mode="HTML"
        )
        return

    group_query = args[0]
    user_query = args[1].lstrip("@")

    # Ищем группу
    group = await database.get_group_by_id(group_query)
    if not group:
        import aiosqlite
        async with aiosqlite.connect(database.db_path) as conn:
            conn.row_factory = aiosqlite.Row
            async with conn.execute("SELECT * FROM groups WHERE LOWER(group_name) = LOWER(?) LIMIT 1", (group_query,)) as cur:
                row = await cur.fetchone()
                if row:
                    group = dict(row)

    if not group:
        await message.answer(f"❌ Группа <code>{group_query}</code> не найдена в базе данных.", parse_mode="HTML")
        return

    group_id = group["id"]
    group_name = group["group_name"]

    target_user = None
    if user_query.isdigit():
        target_uid = int(user_query)
        target_user = await database.get_user(target_uid)
        if not target_user:
            target_user = {"user_id": target_uid, "username": None, "first_name": "Пользователь"}
    else:
        import aiosqlite
        async with aiosqlite.connect(database.db_path) as conn:
            conn.row_factory = aiosqlite.Row
            async with conn.execute("SELECT * FROM users WHERE LOWER(username) = LOWER(?) LIMIT 1", (user_query,)) as cur:
                row = await cur.fetchone()
                if row:
                    target_user = dict(row)

    if not target_user:
        await message.answer(f"❌ Пользователь <code>{user_query}</code> не найден в базе бота.", parse_mode="HTML")
        return

    target_uid = target_user["user_id"]
    target_uname = target_user.get("username")
    target_name = target_user.get("first_name") or target_uname or f"ID {target_uid}"

    await database.set_group_starosta(group_id, target_uid, target_uname, target_name)
    await message.answer(
        f"✅ <b>Староста успешно назначен!</b>\n\n"
        f"👥 Группа: <b>{group_name}</b> (ID: <code>{group_id}</code>)\n"
        f"👑 Староста: <b>{target_name}</b> (ID: <code>{target_uid}</code>)",
        parse_mode="HTML"
    )

    try:
        await message.bot.send_message(
            chat_id=target_uid,
            text=(
                f"🎉 <b>Администратор назначил вас старостой группы {group_name}!</b>\n\n"
                "Теперь вам доступно добавление и управление домашними заданиями через меню группы."
            ),
            parse_mode="HTML"
        )
    except Exception:
        pass


@router.message(Command("remove_starosta"))
async def cmd_admin_remove_starosta(message: Message, database: Database = default_db):
    """
    Команда администратора для снятия старосты:
    /remove_starosta <group_id или название>
    """
    admin_id = message.from_user.id
    if not config.is_admin(admin_id):
        await message.answer("⛔ Доступно только главному администратору бота.")
        return

    args = (message.text or "").split()[1:]
    if not args:
        await message.answer(
            "ℹ️ <b>Использование:</b> <code>/remove_starosta &lt;group_id/название&gt;</code>\n\n"
            "Пример: <code>/remove_starosta ИС-21</code>",
            parse_mode="HTML"
        )
        return

    group_query = args[0]
    group = await database.get_group_by_id(group_query)
    if not group:
        import aiosqlite
        async with aiosqlite.connect(database.db_path) as conn:
            conn.row_factory = aiosqlite.Row
            async with conn.execute("SELECT * FROM groups WHERE LOWER(group_name) = LOWER(?) LIMIT 1", (group_query,)) as cur:
                row = await cur.fetchone()
                if row:
                    group = dict(row)

    if not group:
        await message.answer(f"❌ Группа <code>{group_query}</code> не найдена.", parse_mode="HTML")
        return

    group_id = group["id"]
    group_name = group["group_name"]

    starosta = await database.get_group_starosta(group_id)
    if not starosta:
        await message.answer(f"ℹ️ У группы <b>{group_name}</b> и так нет назначенного старосты.", parse_mode="HTML")
        return

    await database.remove_group_starosta(group_id)
    await message.answer(
        f"✅ <b>Староста группы {group_name} успешно снят!</b>",
        parse_mode="HTML"
    )

    old_st_id = starosta["user_id"]
    try:
        await message.bot.send_message(
            chat_id=old_st_id,
            text=f"ℹ️ Ваш статус старосты группы <b>{group_name}</b> был снят администратором бота.",
            parse_mode="HTML"
        )
    except Exception:
        pass


# -------------------------------------------------------------------------
# ДОМАШНИЕ ЗАДАНИЯ (homework)
# -------------------------------------------------------------------------

@router.message(Command("hw"))
@router.message(Command("homework"))
@router.message(F.text.in_({"📚 ДЗ 📚", "📚 ДЗ", "📚 Homework 📚", "📚 Homework"}))
async def cmd_homework(message: Message, database: Database = default_db):
    """
    Просмотр списка домашних заданий группы с кнопками управления для старосты.
    """
    user_id = message.from_user.id
    user = await database.get_user(user_id)
    lang = user.get("language", "ru") if user else "ru"
    group_id = user.get("group_id") if user else None
    group_name = user.get("group_name", "") if user else ""

    if not group_id:
        await message.answer(
            "⚠️ Сначала выберите вашу группу с помощью кнопки «⚙️ Сменить группу»!"
            if lang == "ru"
            else "⚠️ Please select your group first using «⚙️ Change group»!"
        )
        return

    starosta = await database.get_group_starosta(group_id)
    is_starosta = bool(starosta and starosta["user_id"] == user_id) or config.is_admin(user_id)
    hw_list = await database.get_upcoming_homework(group_id)

    if not hw_list:
        text = (
            f"📚 <b>Домашние задания группы {group_name}</b>\n\n"
            "🎉 Заданий пока нет! Отдыхайте.\n\n"
            "<i>💡 Как это работает: староста группы может добавлять домашние задания. "
            "Они автоматически транслируются всем студентам и прикрепляются к расписанию на завтра!</i>"
            if lang == "ru"
            else f"📚 <b>Homework for group {group_name}</b>\n\n"
            "🎉 No homework assigned yet! Enjoy your day.\n\n"
            "<i>💡 How it works: The group starosta can add homework assignments. "
            "They are automatically broadcast to all students and attached to tomorrow's schedule!</i>"
        )
    else:
        text = (
            f"📚 <b>Актуальные домашние задания группы {group_name}:</b>\n\n"
            if lang == "ru"
            else f"📚 <b>Active homework assignments for {group_name}:</b>\n\n"
        )
        for idx, hw in enumerate(hw_list, start=1):
            text += (
                f"{idx}. 📌 <b>{hw['subject']}</b> (до <code>{hw['due_date']}</code>)\n"
                f"📝 {hw['task_text']}\n\n"
            )
        if not is_starosta:
            text += (
                "<i>💡 Староста группы может добавлять новые задания через меню «🙋‍♂️ Староста».</i>"
                if lang == "ru"
                else "<i>💡 Group starosta can add new assignments via «🙋‍♂️ Starosta».</i>"
            )

    kb = get_homework_list_keyboard(hw_list, is_starosta=is_starosta, lang=lang)
    await message.answer(text, reply_markup=kb, parse_mode="HTML")


@router.callback_query(HomeworkCallback.filter(F.action == "add"))
async def cb_homework_add_start(
    callback: CallbackQuery, state: FSMContext, database: Database = default_db
):
    """
    Шаг 1 создания ДЗ: староста выбирает предмет.
    """
    user_id = callback.from_user.id
    user = await database.get_user(user_id)
    lang = user.get("language", "ru") if user else "ru"
    group_id = user.get("group_id") if user else None

    if not group_id:
        await callback.answer("Сначала выберите группу!", show_alert=True)
        return

    starosta = await database.get_group_starosta(group_id)
    deputy = await database.get_group_deputy(group_id)
    is_starosta_or_deputy = (
        (starosta and starosta["user_id"] == user_id)
        or (deputy and deputy["user_id"] == user_id)
    )
    if not is_starosta_or_deputy and not config.is_admin(user_id):
        await callback.answer("Только староста группы может добавлять домашние задания!", show_alert=True)
        return

    # Защита от флуда: если FSM уже активен — не запускать заново
    current_state = await state.get_state()
    if current_state and current_state.startswith("HomeworkStates:"):
        await callback.answer(
            "⚠️ Вы уже добавляете ДЗ! Завершите или отмените текущий процесс." if lang == "ru"
            else "⚠️ You are already adding homework! Finish or cancel the current process.",
            show_alert=True
        )
        return

    await state.set_state(HomeworkStates.waiting_for_subject)
    await state.update_data(group_id=group_id, group_name=user.get("group_name", group_id), lang=lang)


    subjects_dict = await database.get_group_subjects_and_teachers(group_id)
    subject_names = sorted(list(subjects_dict.keys()))

    if subject_names:
        kb = get_homework_subjects_inline_keyboard(subject_names, lang=lang)
        await state.update_data(available_subjects=subject_names)
        await callback.message.edit_text(
            "📖 <b>Добавление ДЗ (Шаг 1 из 3):</b>\n\n"
            "Выберите предмет из расписания группы ниже или напишите название предмета сообщением:"
            if lang == "ru"
            else "📖 <b>Add Homework (Step 1 of 3):</b>\n\n"
            "Select a subject from your group schedule below or type the subject name:",
            reply_markup=kb,
            parse_mode="HTML"
        )
    else:
        from aiogram.utils.keyboard import InlineKeyboardBuilder
        from aiogram.types import InlineKeyboardButton
        b = InlineKeyboardBuilder()
        b.row(InlineKeyboardButton(text="❌ Отмена", callback_data=HomeworkCallback(action="cancel").pack()))
        await callback.message.edit_text(
            "📖 <b>Добавление ДЗ (Шаг 1 из 3):</b>\n\n"
            "Введите название предмета (например, <i>Математика</i> или <i>Информатика</i>):"
            if lang == "ru"
            else "📖 <b>Add Homework (Step 1 of 3):</b>\n\n"
            "Enter subject name:",
            reply_markup=b.as_markup(),
            parse_mode="HTML"
        )
    await callback.answer()


@router.callback_query(HwSubjectCallback.filter(), HomeworkStates.waiting_for_subject)
async def cb_homework_subject_pick(
    callback: CallbackQuery, callback_data: HwSubjectCallback, state: FSMContext
):
    """
    Выбор предмета нажатием на инлайн-кнопку.
    """
    data = await state.get_data()
    subjects = data.get("available_subjects", [])
    idx = callback_data.idx

    if idx < 0 or idx >= len(subjects):
        await callback.answer("Ошибка выбора предмета.", show_alert=True)
        return

    subject = subjects[idx]
    await state.update_data(subject=subject)
    await state.set_state(HomeworkStates.waiting_for_due_date)

    lang = data.get("lang", "ru")
    kb = get_homework_date_suggestions_keyboard(lang=lang)
    await callback.message.edit_text(
        f"📖 Предмет: <b>{subject}</b>\n\n"
        "📅 <b>Срок сдачи (Шаг 2 из 3):</b>\n"
        "Выберите дату на клавиатуре или введите сообщением в формате <code>ДД.ММ.ГГГГ</code> (например, <code>15.09.2026</code>):"
        if lang == "ru"
        else f"📖 Subject: <b>{subject}</b>\n\n"
        "📅 <b>Due date (Step 2 of 3):</b>\n"
        "Select due date below or type it as <code>DD.MM.YYYY</code>:",
        reply_markup=kb,
        parse_mode="HTML"
    )
    await callback.answer()


@router.message(HomeworkStates.waiting_for_subject)
async def process_hw_subject_text(message: Message, state: FSMContext):
    """
    Ввод названия предмета текстовым сообщением.
    """
    subject = (message.text or "").strip()
    if not subject:
        await message.answer("Пожалуйста, введите корректное название предмета.")
        return

    await state.update_data(subject=subject)
    await state.set_state(HomeworkStates.waiting_for_due_date)

    data = await state.get_data()
    lang = data.get("lang", "ru")
    kb = get_homework_date_suggestions_keyboard(lang=lang)
    await message.answer(
        f"📖 Предмет: <b>{subject}</b>\n\n"
        "📅 <b>Срок сдачи (Шаг 2 из 3):</b>\n"
        "Выберите дату на клавиатуре или введите сообщением в формате <code>ДД.ММ.ГГГГ</code> (например, <code>15.09.2026</code>):"
        if lang == "ru"
        else f"📖 Subject: <b>{subject}</b>\n\n"
        "📅 <b>Due date (Step 2 of 3):</b>\n"
        "Select due date below or type it as <code>DD.MM.YYYY</code>:",
        reply_markup=kb,
        parse_mode="HTML"
    )


@router.callback_query(HwDateCallback.filter(), HomeworkStates.waiting_for_due_date)
async def cb_process_hw_date(
    callback: CallbackQuery, callback_data: HwDateCallback, state: FSMContext
):
    """
    Выбор срока сдачи через инлайн-кнопку.
    """
    due_date = callback_data.date_str
    await state.update_data(due_date=due_date)
    await state.set_state(HomeworkStates.waiting_for_task_text)

    data = await state.get_data()
    subject = data.get("subject", "")
    lang = data.get("lang", "ru")

    from aiogram.utils.keyboard import InlineKeyboardBuilder
    from aiogram.types import InlineKeyboardButton
    b = InlineKeyboardBuilder()
    b.row(InlineKeyboardButton(text="❌ Отмена", callback_data=HomeworkCallback(action="cancel").pack()))

    await callback.message.edit_text(
        f"📖 Предмет: <b>{subject}</b>\n"
        f"📅 Срок сдачи: <b>{due_date}</b>\n\n"
        "📝 <b>Текст задания (Шаг 3 из 3):</b>\n"
        "Введите текст задания (номера задач, параграфы, ссылка и т.д.):"
        if lang == "ru"
        else f"📖 Subject: <b>{subject}</b>\n"
        f"📅 Due date: <b>{due_date}</b>\n\n"
        "📝 <b>Task text (Step 3 of 3):</b>\n"
        "Enter homework details (exercises, pages, links, etc.):",
        reply_markup=b.as_markup(),
        parse_mode="HTML"
    )
    await callback.answer()


@router.message(HomeworkStates.waiting_for_due_date)
async def process_hw_date_text(message: Message, state: FSMContext):
    """
    Ввод срока сдачи сообщением.
    """
    due_date = (message.text or "").strip()
    await state.update_data(due_date=due_date)
    await state.set_state(HomeworkStates.waiting_for_task_text)

    data = await state.get_data()
    subject = data.get("subject", "")
    lang = data.get("lang", "ru")

    from aiogram.utils.keyboard import InlineKeyboardBuilder
    from aiogram.types import InlineKeyboardButton
    b = InlineKeyboardBuilder()
    b.row(InlineKeyboardButton(text="❌ Отмена", callback_data=HomeworkCallback(action="cancel").pack()))

    await message.answer(
        f"📖 Предмет: <b>{subject}</b>\n"
        f"📅 Срок сдачи: <b>{due_date}</b>\n\n"
        "📝 <b>Текст задания (Шаг 3 из 3):</b>\n"
        "Введите текст задания (номера задач, параграфы, ссылка и т.д.):"
        if lang == "ru"
        else f"📖 Subject: <b>{subject}</b>\n"
        f"📅 Due date: <b>{due_date}</b>\n\n"
        "📝 <b>Task text (Step 3 of 3):</b>\n"
        "Enter homework details (exercises, pages, links, etc.):",
        reply_markup=b.as_markup(),
        parse_mode="HTML"
    )


@router.message(HomeworkStates.waiting_for_task_text)
async def process_hw_task_text(
    message: Message, state: FSMContext, database: Database = default_db
):
    """
    Финал добавления ДЗ: сохранение в базу и АВТОМАТИЧЕСКАЯ ТРАНСЛЯЦИЯ всем студентам группы и в чат!
    """
    task_text = (message.text or "").strip()
    if not task_text:
        await message.answer("Пожалуйста, введите текст задания.")
        return

    data = await state.get_data()
    group_id = data.get("group_id")
    group_name = data.get("group_name", group_id)
    subject = data.get("subject", "Предмет")
    due_date = data.get("due_date", "Срок не указан")
    lang = data.get("lang", "ru")
    author_id = message.from_user.id
    author_name = message.from_user.full_name or message.from_user.username or "Староста"

    # Строгая проверка прав старосты
    starosta = await database.get_group_starosta(group_id)
    if not (starosta and starosta["user_id"] == author_id) and not config.is_admin(author_id):
        await state.clear()
        await message.answer(
            "⛔ Только подтвержденный староста группы может добавлять домашние задания."
            if lang == "ru"
            else "⛔ Only the confirmed group starosta can publish homework assignments."
        )
        return

    await state.clear()

    # 1. Сохраняем в БД
    hw_id = await database.add_homework(
        group_id=group_id,
        subject=subject,
        due_date=due_date,
        task_text=task_text,
        author_id=author_id,
    )
    if not hw_id:
        await message.answer(
            "❌ Ошибка при сохранении домашнего задания. Попробуйте ещё раз."
            if lang == "ru"
            else "❌ Error saving homework. Please try again."
        )
        return

    # 2. Автоматическая трансляция задания группе и привязанным беседам
    subscribers = await database.get_subscribers_for_group(group_id)
    # Превью push-уведомления начинается с предмета — студент сразу видит тему
    broadcast_text = (
        f"📚 <b>{subject} — Новое домашнее задание!</b>\n\n"
        f"👥 Группа: <b>{group_name}</b>\n"
        f"📖 Предмет: <b>{subject}</b>\n"
        f"📅 Срок сдачи: <b>{due_date}</b>\n\n"
        f"📝 <b>Задание:</b>\n{task_text}\n\n"
        f"<i>Опубликовал(а): {author_name}</i>"
    )

    delivered_count = 0
    for chat_id, thread_id in subscribers:
        try:
            kwargs = {
                "chat_id": chat_id,
                "text": broadcast_text,
                "parse_mode": "HTML",
                "disable_web_page_preview": True,
            }
            if thread_id is not None:
                kwargs["message_thread_id"] = thread_id
            await message.bot.send_message(**kwargs)
            delivered_count += 1
            await asyncio.sleep(0.04)
        except Exception as ex:
            logger.warning("Не удалось отправить ДЗ в чат %d (thread %s): %s", chat_id, thread_id, ex)

    user = await database.get_user(author_id)
    notif = user.get("notifications_enabled", True) if user else True
    is_admin = config.is_admin(author_id)
    kb = get_main_reply_keyboard(notifications_enabled=notif, is_admin=is_admin, lang=lang)

    confirm_text = (
        f"✅ <b>Домашнее задание успешно сохранено и транслировано!</b>\n\n"
        f"📖 Предмет: <b>{subject}</b>\n"
        f"📅 Срок сдачи: <b>{due_date}</b>\n"
        f"👥 Доставлено получателям: <b>{delivered_count}</b>\n\n"
        "Задание также будет автоматически включено в вечернюю рассылку расписания."
        if lang == "ru"
        else f"✅ <b>Homework successfully saved and broadcast!</b>\n\n"
        f"📖 Subject: <b>{subject}</b>\n"
        f"📅 Due date: <b>{due_date}</b>\n"
        f"👥 Recipients notified: <b>{delivered_count}</b>\n\n"
        "The assignment will also appear in daily schedule broadcasts."
    )
    await message.answer(confirm_text, reply_markup=kb, parse_mode="HTML")


@router.callback_query(HomeworkCallback.filter(F.action == "del_pick"))
async def cb_homework_delete_pick(
    callback: CallbackQuery, database: Database = default_db
):
    """
    Список ДЗ для удаления старостой.
    """
    user_id = callback.from_user.id
    user = await database.get_user(user_id)
    lang = user.get("language", "ru") if user else "ru"
    group_id = user.get("group_id") if user else None

    if not group_id:
        await callback.answer("Сначала выберите группу!", show_alert=True)
        return

    starosta = await database.get_group_starosta(group_id)
    if not (starosta and starosta["user_id"] == user_id) and not config.is_admin(user_id):
        await callback.answer("Только староста группы может удалять домашние задания!", show_alert=True)
        return

    hw_list = await database.get_upcoming_homework(group_id)
    if not hw_list:
        await callback.answer("Нет заданий для удаления.", show_alert=True)
        return

    kb = get_homework_delete_keyboard(hw_list, lang=lang)
    await callback.message.edit_text(
        "🗑 <b>Выберите задание для удаления:</b>" if lang == "ru" else "🗑 <b>Select homework to delete:</b>",
        reply_markup=kb,
        parse_mode="HTML"
    )
    await callback.answer()


@router.callback_query(HomeworkCallback.filter(F.action == "del"))
async def cb_homework_delete_confirm(
    callback: CallbackQuery, callback_data: HomeworkCallback, database: Database = default_db
):
    """
    Подтверждение удаления ДЗ.
    """
    user_id = callback.from_user.id
    user = await database.get_user(user_id)
    lang = user.get("language", "ru") if user else "ru"
    group_id = user.get("group_id") if user else None

    if not group_id:
        await callback.answer("Сначала выберите группу!", show_alert=True)
        return

    starosta = await database.get_group_starosta(group_id)
    if not (starosta and starosta["user_id"] == user_id) and not config.is_admin(user_id):
        await callback.answer("Только староста группы может удалять домашние задания!", show_alert=True)
        return

    hw_id = callback_data.hw_id
    success = await database.delete_homework(hw_id, group_id=group_id)
    if success:
        await callback.answer("Задание успешно удалено!", show_alert=True)
    else:
        await callback.answer("Задание не найдено или уже удалено.", show_alert=True)

    hw_list = await database.get_upcoming_homework(group_id)
    kb = get_homework_list_keyboard(hw_list, is_starosta=True, lang=lang)
    await callback.message.edit_text(
        "✅ Задание удалено. Список актуальных заданий:" if lang == "ru" else "✅ Homework deleted. Active assignments:",
        reply_markup=kb,
        parse_mode="HTML"
    )


@router.callback_query(HomeworkCallback.filter(F.action == "cancel"))
async def cb_homework_cancel(
    callback: CallbackQuery, state: FSMContext, database: Database = default_db
):
    """
    Отмена действий с домашним заданием.
    """
    await state.clear()
    user_id = callback.from_user.id
    user = await database.get_user(user_id)
    lang = user.get("language", "ru") if user else "ru"
    group_id = user.get("group_id") if user else None
    hw_list = await database.get_upcoming_homework(group_id) if group_id else []
    starosta = await database.get_group_starosta(group_id) if group_id else None
    is_starosta = bool(starosta and starosta["user_id"] == user_id)

    kb = get_homework_list_keyboard(hw_list, is_starosta=is_starosta, lang=lang)
    await callback.message.edit_text(
        "❌ Действие отменено." if lang == "ru" else "❌ Action cancelled.",
        reply_markup=kb,
        parse_mode="HTML"
    )
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


@router.message(Command("stats"), StateFilter("*"))
async def cmd_stats(message: Message, state: FSMContext, database: Database = default_db):
    """
    Расширенная статистика бота для администратора (/stats).
    """
    user_id = message.from_user.id
    if not config.is_admin(user_id):
        return

    await state.clear()
    try:
        s = await database.get_bot_stats()
    except Exception as e:
        await message.answer(f"❌ Ошибка получения статистики: {e}")
        return

    top_lines = "\n".join(
        f"  {i+1}. <b>{g['group_name']}</b> — {g['student_count']} студ."
        for i, g in enumerate(s["top_groups"])
    ) or "  —"

    text = (
        "📊 <b>Статистика бота КТМУ</b>\n"
        "════════════════════\n\n"
        "👥 <b>Пользователи:</b>\n"
        f"  • Всего: <code>{s['total_users']}</code>\n"
        f"  • Активны сегодня: <code>{s['active_1d']}</code>\n"
        f"  • Активны за 7 дней: <code>{s['active_7d']}</code>\n"
        f"  • С уведомлениями: <code>{s['notif_enabled']}</code>\n\n"
        "🏛 <b>Инфраструктура:</b>\n"
        f"  • Групп в базе: <code>{s['total_groups']}</code>\n"
        f"  • Старост назначено: <code>{s['total_starostas']}</code>\n"
        f"  • Групповых чатов: <code>{s['total_chats']}</code>\n\n"
        "📚 <b>Контент:</b>\n"
        f"  • Домашних заданий: <code>{s['total_hw']}</code>\n"
        f"  • Активных заметок: <code>{s['active_notes']}</code>\n\n"
        "🏆 <b>Топ-5 групп по студентам:</b>\n"
        f"{top_lines}\n"
    )
    await message.answer(text, parse_mode="HTML")


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

    elif action == "export_db":
        await callback.answer("⏳ Подготовка базы данных...")
        if not os.path.exists(database.db_path):
            await callback.message.answer("❌ Файл базы данных не найден на сервере.")
            return
        now_str = datetime.now().strftime("%Y%m%d_%H%M%S")
        file_size_kb = os.path.getsize(database.db_path) / 1024
        doc = FSInputFile(database.db_path, filename=f"ktmu_bot_backup_{now_str}.db")
        await callback.message.answer_document(
            document=doc,
            caption=(
                f"💾 <b>Резервная копия базы данных КТМУ</b>\n\n"
                f"📁 Файл: <code>{os.path.basename(database.db_path)}</code>\n"
                f"📦 Размер: <code>{file_size_kb:.1f} КБ</code>\n"
                f"⏰ Дата: <code>{datetime.now().strftime('%d.%m.%Y %H:%M:%S')}</code>\n\n"
                f"💡 Сохраните этот файл. Его можно загрузить на хост или восстановить кнопкой «📥 Загрузить БД»."
            ),
            parse_mode="HTML",
        )

    elif action == "import_db":
        await state.set_state(AdminStates.waiting_for_db_file)
        kb = get_admin_back_inline_keyboard()
        await callback.message.edit_text(
            "📥 <b>Загрузка / Восстановление базы данных</b>\n\n"
            "Отправьте файл базы данных (с расширением <code>.db</code> или <code>.sqlite</code>) "
            "прямо сюда в диалог <b>документом (без сжатия)</b>.\n\n"
            "⚠️ <b>Внимание:</b> Текущая база данных будет автоматически сохранена в резервную копию "
            "<code>.bak</code> перед заменой.\n\n"
            "<i>Для отмены нажмите кнопку ниже:</i>",
            reply_markup=kb,
            parse_mode="HTML",
        )
        await callback.answer()

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
    chats = await database.get_all_chats()
    total_recipients = len(user_ids) + len(chats)
    if total_recipients == 0:
        await message.answer("⚠️ В базе данных пока нет зарегистрированных получателей.")
        return

    status_msg = await message.answer(
        f"🚀 <b>Начало рассылки:</b> получателей {total_recipients} (пользователей: {len(user_ids)}, чатов/тем: {len(chats)})...\nПожалуйста, подождите.",
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

    for c in chats:
        cid = c["chat_id"]
        mtid = c.get("message_thread_id")
        try:
            await message.send_copy(chat_id=cid, message_thread_id=mtid)
            success_count += 1
        except TelegramForbiddenError:
            blocked_count += 1
            await database.set_chat_notifications(cid, mtid, False)
        except Exception as e:
            logger.warning("Ошибка при рассылке в чат %d (thread %s): %s", cid, mtid, e)
            error_count += 1
        await asyncio.sleep(0.04)

    kb = get_admin_back_inline_keyboard()
    await status_msg.edit_text(
        "✅ <b>Рассылка успешно завершена!</b>\n\n"
        f"👥 Всего получателей: <code>{total_recipients}</code>\n"
        f"📤 Успешно доставлено: <code>{success_count}</code>\n"
        f"🚫 Заблокировали бота / чат недоступен: <code>{blocked_count}</code>\n"
        f"❌ Ошибок отправки: <code>{error_count}</code>",
        reply_markup=kb,
        parse_mode="HTML",
    )


@router.message(Command("backup_db"))
@router.message(Command("export_db"))
async def cmd_backup_db(message: Message, database: Database = default_db):
    """Отправляет файл базы данных администратору."""
    user_id = message.from_user.id
    if not config.is_admin(user_id):
        return
    if not os.path.exists(database.db_path):
        await message.answer("❌ Файл базы данных не найден на сервере.")
        return
    now_str = datetime.now().strftime("%Y%m%d_%H%M%S")
    file_size_kb = os.path.getsize(database.db_path) / 1024
    doc = FSInputFile(database.db_path, filename=f"ktmu_bot_backup_{now_str}.db")
    await message.answer_document(
        document=doc,
        caption=(
            f"💾 <b>Резервная копия базы данных КТМУ</b>\n\n"
            f"📁 Файл: <code>{os.path.basename(database.db_path)}</code>\n"
            f"📦 Размер: <code>{file_size_kb:.1f} КБ</code>\n"
            f"⏰ Дата: <code>{datetime.now().strftime('%d.%m.%Y %H:%M:%S')}</code>\n\n"
            f"💡 Для восстановления отправьте этот файл боту или используйте команду /restore_db."
        ),
        parse_mode="HTML",
    )


@router.message(Command("restore_db"))
@router.message(Command("import_db"))
async def cmd_restore_db(message: Message, state: FSMContext):
    """Переводит бота в режим ожидания файла БД для восстановления."""
    user_id = message.from_user.id
    if not config.is_admin(user_id):
        return
    await state.set_state(AdminStates.waiting_for_db_file)
    kb = get_admin_back_inline_keyboard()
    await message.answer(
        "📥 <b>Загрузка / Восстановление базы данных</b>\n\n"
        "Отправьте файл базы данных (с расширением <code>.db</code> или <code>.sqlite</code>) "
        "документом в этот диалог.\n\n"
        "⚠️ <b>Внимание:</b> Текущая база данных будет автоматически забэкаплена перед заменой.\n\n"
        "<i>Для отмены отправьте /cancel или откройте /admin.</i>",
        reply_markup=kb,
        parse_mode="HTML",
    )


@router.message(AdminStates.waiting_for_db_file, F.document)
@router.message(F.document & (F.caption == "/restore_db"))
async def process_admin_restore_db(
    message: Message,
    state: FSMContext,
    bot: Bot,
    database: Database = default_db,
):
    """Принимает файл БД, проверяет его целостность и восстанавливает базу."""
    user_id = message.from_user.id
    if not config.is_admin(user_id):
        await state.clear()
        return

    doc = message.document
    fname = (doc.file_name or "").lower()
    if not (fname.endswith(".db") or fname.endswith(".sqlite")):
        await message.answer(
            "❌ <b>Некорректный формат файла.</b>\n"
            "Файл должен иметь расширение <code>.db</code> или <code>.sqlite</code> (например, <code>ktmu_bot.db</code>).\n"
            "Пожалуйста, отправьте корректный файл:"
        )
        return

    status_msg = await message.answer("⏳ <i>Скачивание и валидация базы данных...</i>", parse_mode="HTML")
    temp_path = f"{database.db_path}.tmp_import"

    try:
        tg_file = await bot.get_file(doc.file_id)
        await bot.download_file(tg_file.file_path, destination=temp_path)

        # Проверяем валидность SQLite
        conn = sqlite3.connect(temp_path)
        try:
            cur = conn.cursor()
            cur.execute("PRAGMA integrity_check;")
            row = cur.fetchone()
            if not row or row[0] != "ok":
                raise ValueError("Файл поврежден (нарушена целостность структуры SQLite).")

            cur.execute("SELECT name FROM sqlite_master WHERE type='table';")
            tables = {r[0] for r in cur.fetchall()}
            if "users" not in tables:
                raise ValueError("В базе отсутствует обязательная таблица 'users'.")
        finally:
            conn.close()

        # Резервное копирование старой БД
        if os.path.exists(database.db_path):
            shutil.copy2(database.db_path, f"{database.db_path}.bak")

        # Заменяем текущую БД новым файлом
        shutil.copy2(temp_path, database.db_path)
        if os.path.exists(temp_path):
            os.remove(temp_path)

        # Переинициализация и очистка кэшей
        await database.init_db()
        timetable_parser.clear_ram_cache()
        clear_ram_image_cache()

        stats = await database.get_admin_stats()
        await state.clear()

        await status_msg.edit_text(
            "✅ <b>База данных успешно загружена и восстановлена!</b>\n\n"
            f"👥 Всего пользователей: <code>{stats['total_users']}</code>\n"
            f"👤 Личных диалогов: <code>{stats.get('private_users', stats['total_users'])}</code>\n"
            f"💬 Групповых чатов: <code>{stats.get('group_chats', 0)}</code>\n"
            f"🎓 С выбранной группой: <code>{stats['with_group']}</code>\n"
            f"🔔 С включенными рассылками: <code>{stats['notifications_on']}</code>\n"
            f"🏛 Групп колледжа: <code>{stats['total_groups']}</code>\n\n"
            "Все пользователи, настройки и расписания восстановлены!",
            parse_mode="HTML",
        )
    except Exception as e:
        if os.path.exists(temp_path):
            try:
                os.remove(temp_path)
            except Exception:
                pass
        await status_msg.edit_text(
            f"❌ <b>Ошибка при восстановлении базы данных:</b>\n<code>{e}</code>\n\n"
            "Текущая база данных осталась без изменений.",
            parse_mode="HTML",
        )


# -------------------------------------------------------------------------
# ЗВОНКИ, ЧАТ ГРУППЫ, НАВИГАТОР, ПРЕПОДАВАТЕЛИ, СЕССИЯ, ЗАМЕТКИ, РЕГУЛИРОВКА
# -------------------------------------------------------------------------

def format_remaining_time(minutes: int, lang: str = "ru") -> str:
    """
    Форматирует оставшееся время:
    - Если >= 60 минут: часы и минуты (например, '7 ч 34 мин' или '2 ч').
    - Если < 60 минут: только минуты (например, '25 мин').
    """
    if minutes < 0:
        minutes = 0
    if minutes >= 60:
        hours = minutes // 60
        mins = minutes % 60
        if mins > 0:
            return f"{hours} ч {mins} мин" if lang == "ru" else f"{hours} h {mins} min"
        else:
            return f"{hours} ч" if lang == "ru" else f"{hours} h"
    return f"{minutes} мин" if lang == "ru" else f"{minutes} min"


def get_bells_card_text(lang: str = "ru") -> str:
    """Генерирует карточку звонков с динамическим статусом текущей пары колледжа."""
    now_dt = get_current_college_time()
    cur_time_str = now_dt.strftime("%H:%M")
    cur_time = now_dt.time()

    cur_minutes = cur_time.hour * 60 + cur_time.minute
    first_start_min = 8 * 60 + 30
    last_end_min = 20 * 60 + 30

    if cur_minutes < first_start_min:
        diff_min = first_start_min - cur_minutes
        time_str = format_remaining_time(diff_min, lang)
        status_text = (
            f"☕ <b>Пары еще не начались</b>\nДо 1-й пары осталось: <b>{time_str}</b>"
            if lang == "ru"
            else f"☕ <b>Classes haven't started yet</b>\nTime to 1st class: <b>{time_str}</b>"
        )
    elif cur_minutes >= last_end_min:
        status_text = (
            "🌙 <b>Учебный день завершен!</b> Все пары на сегодня закончились."
            if lang == "ru"
            else "🌙 <b>School day finished!</b> All classes have ended."
        )
    else:
        found = False
        for b in BELLS_TIMETABLE:
            p_num = b["pair"]
            s_parts = [int(x) for x in b["start"].split(":")]
            e_parts = [int(x) for x in b["end"].split(":")]
            p_start_min = s_parts[0] * 60 + s_parts[1]
            p_end_min = e_parts[0] * 60 + e_parts[1]

            if p_start_min <= cur_minutes < p_end_min:
                rem_min = p_end_min - cur_minutes
                time_str = format_remaining_time(rem_min, lang)
                status_text = (
                    f"🟢 <b>Сейчас идет {p_num}-я пара</b> ({b['start']} – {b['end']})\n"
                    f"⏳ До звонка с пары: <b>{time_str}</b>"
                    if lang == "ru"
                    else f"🟢 <b>Currently class {p_num}</b> ({b['start']} – {b['end']})\n"
                    f"⏳ Bell in: <b>{time_str}</b>"
                )
                found = True
                break

        if not found:
            for i in range(len(BELLS_TIMETABLE) - 1):
                cur_b = BELLS_TIMETABLE[i]
                next_b = BELLS_TIMETABLE[i + 1]
                cur_e_parts = [int(x) for x in cur_b["end"].split(":")]
                next_s_parts = [int(x) for x in next_b["start"].split(":")]
                b_start_min = cur_e_parts[0] * 60 + cur_e_parts[1]
                b_end_min = next_s_parts[0] * 60 + next_s_parts[1]

                if b_start_min <= cur_minutes < b_end_min:
                    rem_min = b_end_min - cur_minutes
                    time_str = format_remaining_time(rem_min, lang)
                    break_desc = (
                        "🍽 <b>Большая перемена (обед 40 мин)</b>"
                        if cur_b.get("is_big_break")
                        else "🟡 <b>Перемена (10 мин)</b>"
                    ) if lang == "ru" else (
                        "🍽 <b>Big break / lunch (40 min)</b>"
                        if cur_b.get("is_big_break")
                        else "🟡 <b>Break (10 min)</b>"
                    )
                    status_text = (
                        f"{break_desc}\n"
                        f"⏳ До начала {next_b['pair']}-й пары: <b>{time_str}</b>"
                        if lang == "ru"
                        else f"{break_desc}\n"
                        f"⏳ Class {next_b['pair']} starts in: <b>{time_str}</b>"
                    )
                    break


    title = get_text("bells_title", lang)
    clock_line = (
        f"🕒 <i>Текущее время: {cur_time_str}</i>\n\n"
        if lang == "ru"
        else f"🕒 <i>Current time: {cur_time_str}</i>\n\n"
    )
    card = get_text("bells_card", lang)
    return f"{title}{clock_line}{card}{status_text}"


@router.message(Command("bells"))
@router.message(F.text.in_({"⏰ Звонки", "⏰ Bells"}))
async def cmd_bells(message: Message, database: Database = default_db):
    """Карточка расписания звонков с динамическим статусом текущей пары."""
    user = await database.get_user(message.from_user.id)
    lang = user.get("language", "ru") if user else "ru"
    text = get_bells_card_text(lang)
    kb = get_bells_inline_keyboard(lang)
    await message.answer(text, reply_markup=kb, parse_mode="HTML")


@router.callback_query(BellsCallback.filter())
async def cb_bells(callback: CallbackQuery, callback_data: BellsCallback, database: Database = default_db):
    """Обновление карточки звонков."""
    user = await database.get_user(callback.from_user.id)
    lang = user.get("language", "ru") if user else "ru"
    if callback_data.action == "refresh":
        text = get_bells_card_text(lang)
        kb = get_bells_inline_keyboard(lang)
        try:
            await callback.message.edit_text(text, reply_markup=kb, parse_mode="HTML")
            await callback.answer("Статус обновлен!" if lang == "ru" else "Status refreshed!")
        except Exception:
            await callback.answer()


# -------------------------------------------------------------------------
# ЧАТ ГРУППЫ (Ссылка на беседу)
# -------------------------------------------------------------------------

@router.message(Command("group_chat"))
@router.message(F.text.in_({"💬 Чат группы", "💬 Group chat"}))
async def cmd_group_chat(message: Message, database: Database = default_db):
    """Просмотр ссылки на официальную беседу группы в Telegram/VK."""
    user = await _get_authorized_user(message, database)
    if not user:
        return

    group_id = user["group_id"]
    group_name = user.get("group_name", "не выбрана")
    lang = user.get("language", "ru")
    user_id = message.from_user.id

    is_starosta = await database.is_user_starosta_of_group(user_id, group_id) or config.is_admin(user_id)
    chat_info = await database.get_group_chat_link(group_id)
    chat_link = chat_info["chat_link"] if chat_info else None

    title = get_text("group_chat_title", lang, group_name=group_name)
    if chat_link:
        text = (
            f"{title}🔗 <b>Ссылка на беседу:</b>\n{chat_link}\n\n"
            "<i>Нажмите кнопку ниже, чтобы присоединиться к беседе одногруппников!</i>"
            if lang == "ru"
            else f"{title}🔗 <b>Group chat link:</b>\n{chat_link}\n\n"
            "<i>Click the button below to join your classmates!</i>"
        )
    else:
        text = title + get_text("group_chat_none", lang)

    kb = get_group_chat_link_keyboard(chat_link=chat_link, is_starosta=is_starosta, lang=lang)
    await message.answer(text, reply_markup=kb, parse_mode="HTML", disable_web_page_preview=True)


@router.callback_query(GroupChatLinkCallback.filter())
async def cb_group_chat_link(
    callback: CallbackQuery, callback_data: GroupChatLinkCallback, state: FSMContext, database: Database = default_db
):
    """Управление ссылкой на чат группы старостой."""
    user = await database.get_user(callback.from_user.id)
    if not user or not user.get("group_id"):
        await callback.answer("Сначала выберите группу!", show_alert=True)
        return

    group_id = user["group_id"]
    lang = user.get("language", "ru")
    user_id = callback.from_user.id
    is_starosta = await database.is_user_starosta_of_group(user_id, group_id) or config.is_admin(user_id)

    if not is_starosta:
        await callback.answer(
            "Только староста группы или администратор может менять ссылку!" if lang == "ru" else "Only starosta can edit chat link!",
            show_alert=True
        )
        return

    if callback_data.action == "set":
        await state.set_state(GroupChatLinkStates.waiting_for_url)
        await callback.message.answer(
            get_text("group_chat_prompt_url", lang),
            parse_mode="HTML"
        )
        await callback.answer()
    elif callback_data.action == "del":
        await database.delete_group_chat_link(group_id)
        await callback.answer(get_text("group_chat_deleted", lang))
        kb = get_group_chat_link_keyboard(chat_link=None, is_starosta=is_starosta, lang=lang)
        group_name = user.get("group_name", "")
        text = get_text("group_chat_title", lang, group_name=group_name) + get_text("group_chat_none", lang)
        await callback.message.edit_text(text, reply_markup=kb, parse_mode="HTML")


@router.message(GroupChatLinkStates.waiting_for_url)
async def process_group_chat_url(message: Message, state: FSMContext, database: Database = default_db):
    """Обработка ввода ссылки на беседу группы."""
    user = await database.get_user(message.from_user.id)
    lang = user.get("language", "ru") if user else "ru"
    url = (message.text or "").strip()

    if not (url.startswith("http://") or url.startswith("https://") or url.startswith("t.me/") or url.startswith("vk.com/") or url.startswith("vk.me/")):
        await message.answer(
            "⚠️ Пожалуйста, отправьте корректную ссылку на чат Telegram или VK (начиная с https://t.me/ или https://vk.me/)."
        )
        return

    if not url.startswith("http"):
        url = "https://" + url

    await state.clear()
    group_id = user["group_id"]
    await database.set_group_chat_link(group_id, url, message.from_user.id)

    is_starosta = True
    kb = get_group_chat_link_keyboard(chat_link=url, is_starosta=is_starosta, lang=lang)
    group_name = user.get("group_name", "")
    title = get_text("group_chat_title", lang, group_name=group_name)
    success_text = get_text("group_chat_saved", lang)
    text = (
        f"{title}{success_text}\n\n"
        f"🔗 <b>Ссылка:</b> {url}\n\n"
        "<i>Теперь любой студент вашей группы сможет попасть в беседу в 1 клик!</i>"
    )
    await message.answer(text, reply_markup=kb, parse_mode="HTML", disable_web_page_preview=True)


# -------------------------------------------------------------------------
# КОРПУСА И КАБИНЕТЫ (Навигатор)
# -------------------------------------------------------------------------

CAMPUS_FLOORS: dict[int, dict[str, str]] = {
    1: {
        "title": "🏢 <b>1 этаж (Главный корпус)</b>",
        "description": (
            "• <b>Центральный вход и пост охраны</b> — пропускной режим, турникеты\n"
            "• <b>Гардероб</b> — прием верхней одежды и сменной обуви\n"
            "• <b>Столовая и буфет</b> (правое крыло) — горячее питание, выпечка, напитки (09:00–16:30)\n"
            "• <b>Спортивный комплекс</b> (левое крыло) — большой спортзал, тренажерный зал, раздевалки\n"
            "• <b>Медицинский пункт</b> (каб. 104) — первая помощь, справки\n"
            "• <b>Учебные мастерские</b> (каб. 110–115) — слесарные и электромонтажные мастерские"
        ),
    },
    2: {
        "title": "🏢 <b>2 этаж (Администрация и лекционные залы)</b>",
        "description": (
            "• <b>Деканат / Учебная часть</b> (каб. 201) — справки об обучении, успеваемость, расписание\n"
            "• <b>Кабинет директора и приемная</b> (каб. 204)\n"
            "• <b>Преподавательская / Учительская</b> (каб. 208) — кафедры, консультации преподавателей\n"
            "• <b>Актовый зал</b> — концерты, собрания, конференции\n"
            "• <b>Учебные аудитории 202–220</b> — лекции и семинары"
        ),
    },
    3: {
        "title": "🏢 <b>3 этаж (Библиотека и IT-лаборатории)</b>",
        "description": (
            "• <b>Библиотека и читальный зал</b> (каб. 301) — выдача учебников, тихая зона, компьютеры\n"
            "• <b>Компьютерные классы</b> (каб. 304, 306, 308) — лаборатории программирования и сетей\n"
            "• <b>Аудитории 302, 303, 305, 307, 309–325</b> — математические и гуманитарные дисциплины"
        ),
    },
    4: {
        "title": "🏢 <b>4 этаж (Естественно-научные лаборатории)</b>",
        "description": (
            "• <b>Лаборатория физики и электротехники</b> (каб. 402)\n"
            "• <b>Лаборатория химии и экологии</b> (каб. 405)\n"
            "• <b>Аудитории специальных дисциплин 401, 403, 404, 406–418</b>\n"
            "• <b>Методический кабинет</b> (каб. 410)"
        ),
    },
}

CAMPUS_ROOMS: list[dict[str, Any]] = [
    {"keys": ["101", "102", "103", "110", "111", "112", "113", "114", "115", "мастерск"], "name": "Учебные мастерские (слесарные / электромонтажные)", "floor": 1, "wing": "1 этаж, коридор мастерских", "desc": "Практические занятия и лабораторные работы."},
    {"keys": ["104", "мед", "медпункт", "врач", "доктор"], "name": "Медицинский пункт (каб. 104)", "floor": 1, "wing": "1 этаж, около центрального холла", "desc": "Первая медицинская помощь, регистрация и выдача справок."},
    {"keys": ["столов", "буфет", "обед", "кушать", "еда", "питани"], "name": "Столовая и буфет", "floor": 1, "wing": "1 этаж, правое крыло", "desc": "Горячие обеды, выпечка, чай и кофе. Время работы: 09:00 – 16:30."},
    {"keys": ["спорт", "спортзал", "зал", "тренажер", "физра", "физкультур"], "name": "Спортивный зал и тренажерный зал", "floor": 1, "wing": "1 этаж, левое крыло", "desc": "Занятия физической культурой, спортивные секции, раздевалки с душевыми."},
    {"keys": ["гардероб", "одежд", "куртк"], "name": "Гардероб", "floor": 1, "wing": "1 этаж, центральный вестибюль", "desc": "Прием и хранение верхней одежды и сменной обуви."},
    {"keys": ["охран", "вход", "проходн", "турникет"], "name": "Пост охраны и проходная", "floor": 1, "wing": "1 этаж, центральный вход", "desc": "Пропускной режим по студенческим билетам."},
    {"keys": ["201", "деканат", "учебн", "учебная часть", "справк"], "name": "Деканат / Учебная часть (каб. 201)", "floor": 2, "wing": "2 этаж, около центральной лестницы", "desc": "Заказ справок об обучении, вопросы по успеваемости и студенческим билетам."},
    {"keys": ["204", "директор", "приемн"], "name": "Кабинет директора и приемная (каб. 204)", "floor": 2, "wing": "2 этаж, центральное фойе", "desc": "Прием по предварительной записи."},
    {"keys": ["208", "учительск", "преподавательск", "кафедр"], "name": "Преподавательская (каб. 208)", "floor": 2, "wing": "2 этаж, правое крыло", "desc": "Рабочие места преподавателей, консультации перед парами и зачетами."},
    {"keys": ["акт", "актовый", "актовый зал", "концерт"], "name": "Актовый зал", "floor": 2, "wing": "2 этаж, центральное фойе", "desc": "Торжественные мероприятия, линейки, концерты колледжа."},
    {"keys": ["301", "библио", "библиотек", "книг", "читальн"], "name": "Библиотека и читальный зал (каб. 301)", "floor": 3, "wing": "3 этаж, левое крыло", "desc": "Выдача учебной литературы, читальный зал, рабочие места с компьютерами и Wi-Fi."},
    {"keys": ["304", "306", "308", "информ", "комп", "компьютер", "ит", "it", "лаб"], "name": "Компьютерные классы (каб. 304, 306, 308)", "floor": 3, "wing": "3 этаж, правое крыло", "desc": "Лаборатории программирования, веб-разработки и администрирования сетей."},
    {"keys": ["402", "физик"], "name": "Лаборатория физики и электротехники (каб. 402)", "floor": 4, "wing": "4 этаж, левое крыло", "desc": "Лабораторные работы по физике и электрооборудованию."},
    {"keys": ["405", "хим", "биолог"], "name": "Лаборатория химии и экологии (каб. 405)", "floor": 4, "wing": "4 этаж, правое крыло", "desc": "Практические опыты по органической и неорганической химии."},
]


@router.message(Command("campus"))
async def cmd_campus(message: Message, database: Database = default_db):
    """Справочник-навигатор по колледжу КТМУ (временно скрыт)."""
    await message.answer("ℹ️ Раздел «Корпуса и кабинеты» временно скрыт.")


async def cb_campus(
    callback: CallbackQuery, callback_data: CampusCallback, state: FSMContext, database: Database = default_db
):
    """Переключение этажей и поиск в навигаторе колледжа."""
    user = await database.get_user(callback.from_user.id)
    lang = user.get("language", "ru") if user else "ru"

    if callback_data.action == "floor":
        floor = callback_data.floor
        floor_data = CAMPUS_FLOORS.get(floor, CAMPUS_FLOORS[1])
        title = get_text("campus_title", lang)
        text = f"{title}\n\n{floor_data.get('title', '')}\n{floor_data.get('description', '')}"
        kb = get_campus_inline_keyboard(current_floor=floor, lang=lang, webapp_url=config.WEBAPP_CAMPUS_URL or None)
        try:
            await callback.message.edit_text(text, reply_markup=kb, parse_mode="HTML")
        except Exception:
            pass
        await callback.answer()
    elif callback_data.action == "search":
        await state.set_state(CampusStates.waiting_for_search_query)
        prompt = get_text("campus_search_prompt", lang)
        await callback.message.answer(prompt, parse_mode="HTML")
        await callback.answer()


async def process_campus_search(message: Message, state: FSMContext, database: Database = default_db):
    """Поиск аудитории или кабинета по номеру или названию."""
    await state.clear()
    query = (message.text or "").strip().lower()
    user = await database.get_user(message.from_user.id)
    lang = user.get("language", "ru") if user else "ru"

    matched_item = None
    for item in CAMPUS_ROOMS:
        if any(k in query for k in item["keys"]):
            matched_item = item
            break

    kb = get_campus_inline_keyboard(
        current_floor=matched_item["floor"] if matched_item else 1,
        lang=lang,
        webapp_url=config.WEBAPP_CAMPUS_URL or None,
    )

    if matched_item:
        text = (
            f"📍 <b>Найдено: {matched_item['name']}</b>\n\n"
            f"🏢 <b>Расположение:</b> {matched_item['wing']}\n"
            f"ℹ️ <b>Описание:</b> {matched_item['desc']}"
        )
    elif query.isdigit() and len(query) == 3:
        fl = int(query[0])
        if 1 <= fl <= 4:
            text = (
                f"📍 <b>Аудитория {query}</b>\n\n"
                f"🏢 <b>Расположение:</b> {fl} этаж\n"
                f"ℹ️ <b>Описание:</b> Учебная аудитория для лекций и практических занятий."
            )
            kb = get_campus_inline_keyboard(
                current_floor=fl,
                lang=lang,
                webapp_url=config.WEBAPP_CAMPUS_URL or None,
            )
        else:
            text = f"⚠️ Аудитория с номером <b>{query}</b> не найдена в корпусах колледжа (в главном корпусе 4 этажа)."
    else:
        text = (
            f"⚠️ По запросу «<b>{message.text}</b>» ничего не найдено.\n\n"
            "💡 <i>Попробуйте ввести номер кабинета (например, 304, 201) или ключевое слово (столовая, спортзал, деканат, библиотека).</i>"
        )

    await message.answer(text, reply_markup=kb, parse_mode="HTML")


# -------------------------------------------------------------------------
# МОИ ПРЕПОДАВАТЕЛИ
# -------------------------------------------------------------------------

@router.message(Command("my_teachers"))
@router.message(F.text.in_({"🌟 Мои преподаватели", "🌟 My teachers"}))
async def cmd_my_teachers(message: Message, database: Database = default_db):
    """Список преподавателей группы по текущему расписанию с предметами."""
    user = await _get_authorized_user(message, database)
    if not user:
        return

    group_id = user["group_id"]
    group_name = user.get("group_name", "не выбрана")
    group_url = user.get("group_url", "")
    lang = user.get("language", "ru")
    thread_id = getattr(message, "message_thread_id", None)
    await send_chat_action_safe(message.bot, message.chat.id, ChatAction.TYPING, thread_id)

    wait_msg = await message.answer("🔍 <i>Загружаю список преподавателей вашей группы...</i>", parse_mode="HTML")

    try:
        week_sched = await timetable_parser.fetch_week_schedule(
            group_id=group_id,
            group_url=group_url,
            anchor_date=date.today(),
            force_refresh=False,
        )
    except Exception as e:
        logger.error("Ошибка загрузки расписания для преподавателей: %s", e)
        week_sched = []

    teacher_subjects: dict[str, set[str]] = {}
    for day in week_sched:
        for lesson in day.get("lessons", []):
            teacher = (lesson.get("teacher") or "").strip()
            subj = (lesson.get("subject") or "").strip()
            if teacher and teacher != "—" and teacher.lower() not in ("нет", "преподаватель"):
                teacher_subjects.setdefault(teacher, set())
                if subj:
                    teacher_subjects[teacher].add(subj)

    if not teacher_subjects:
        await wait_msg.edit_text(get_text("my_teachers_empty", lang))
        return

    teachers_list: list[dict[str, Any]] = []
    lines = [get_text("my_teachers_title", lang, group_name=group_name)]

    for idx, (t_name, subjs) in enumerate(sorted(teacher_subjects.items()), start=1):
        subj_str = ", ".join(sorted(subjs)) if subjs else "Предмет по расписанию"
        lines.append(f"{idx}. 👨‍🏫 <b>{t_name}</b>\n   📖 <i>{subj_str}</i>\n")
        teachers_list.append({"name": t_name, "subject": subj_str})

    lines.append("👇 <i>Нажмите на преподавателя ниже, чтобы посмотреть его полное расписание занятий:</i>")
    text = "\n".join(lines)
    kb = get_my_teachers_keyboard(teachers=teachers_list, lang=lang)
    await wait_msg.edit_text(text, reply_markup=kb, parse_mode="HTML")


@router.callback_query(MyTeacherCallback.filter())
async def cb_my_teacher(callback: CallbackQuery, callback_data: MyTeacherCallback, database: Database = default_db):
    """Быстрый просмотр расписания преподавателя по клику."""
    user = await database.get_user(callback.from_user.id)
    if not user or not user.get("group_id"):
        await callback.answer("Сначала выберите группу!", show_alert=True)
        return

    group_id = user["group_id"]
    group_url = user.get("group_url", "")
    thread_id = getattr(callback.message, "message_thread_id", None)
    await send_chat_action_safe(callback.bot, callback.message.chat.id, ChatAction.TYPING, thread_id)

    try:
        week_sched = await timetable_parser.fetch_week_schedule(
            group_id=group_id,
            group_url=group_url,
            anchor_date=date.today(),
            force_refresh=False,
        )
    except Exception:
        week_sched = []

    teacher_names = set()
    for day in week_sched:
        for lesson in day.get("lessons", []):
            t = (lesson.get("teacher") or "").strip()
            if t and t != "—" and t.lower() not in ("нет", "преподаватель"):
                teacher_names.add(t)

    sorted_teachers = sorted(teacher_names)
    if 0 <= callback_data.idx < len(sorted_teachers):
        target_teacher = sorted_teachers[callback_data.idx]
        await callback.answer()
        await _execute_teacher_search(callback.message, target_teacher, database)
    else:
        await callback.answer("Преподаватель не найден.", show_alert=True)


# -------------------------------------------------------------------------
# СЕССИЯ / ЭКЗАМЕНЫ
# -------------------------------------------------------------------------

@router.message(Command("exams"))
@router.message(F.text.in_({"📅 Экзамены / Сессия", "📅 Exams / Session", "📅 Экзамены", "📅 Сессия"}))
async def cmd_exams(message: Message, database: Database = default_db):
    """Расписание экзаменов, зачетов и консультаций сессии."""
    user = await _get_authorized_user(message, database)
    if not user:
        return

    group_id = user["group_id"]
    group_name = user.get("group_name", "не выбрана")
    lang = user.get("language", "ru")
    user_id = message.from_user.id

    is_starosta = await database.is_user_starosta_of_group(user_id, group_id) or config.is_admin(user_id)
    exams = await database.get_group_exams(group_id)

    title = get_text("exams_title", lang, group_name=group_name)
    if not exams:
        text = title + get_text("exams_empty", lang)
    else:
        lines = [title]
        for idx, ex in enumerate(exams, start=1):
            subj = ex["subject"]
            e_dt = ex["exam_date"]
            e_tm = f", {ex['exam_time']}" if ex.get("exam_time") else ""
            room = f" | каб. {ex['room']}" if ex.get("room") else ""
            tchr = f"\n   👨‍🏫 <i>{ex['teacher']}</i>" if ex.get("teacher") else ""
            lines.append(f"{idx}. 📌 <b>{subj}</b>\n   📅 {e_dt}{e_tm}{room}{tchr}\n")
        text = "\n".join(lines)

    kb = get_exams_list_keyboard(exams=exams, is_starosta=is_starosta, lang=lang)
    await message.answer(text, reply_markup=kb, parse_mode="HTML")


@router.callback_query(ExamsCallback.filter())
async def cb_exams(
    callback: CallbackQuery, callback_data: ExamsCallback, state: FSMContext, database: Database = default_db
):
    """Управление расписанием сессии старостой."""
    user = await database.get_user(callback.from_user.id)
    if not user or not user.get("group_id"):
        await callback.answer("Сначала выберите группу!", show_alert=True)
        return

    group_id = user["group_id"]
    lang = user.get("language", "ru")
    user_id = callback.from_user.id
    is_starosta = await database.is_user_starosta_of_group(user_id, group_id) or config.is_admin(user_id)

    if callback_data.action == "cancel":
        await state.clear()
        exams = await database.get_group_exams(group_id)
        kb = get_exams_list_keyboard(exams=exams, is_starosta=is_starosta, lang=lang)
        group_name = user.get("group_name", "")
        title = get_text("exams_title", lang, group_name=group_name)
        text = title + ("\n".join([f"• <b>{e['subject']}</b> ({e['exam_date']})" for e in exams]) if exams else get_text("exams_empty", lang))
        await callback.message.edit_text(text, reply_markup=kb, parse_mode="HTML")
        await callback.answer()
        return

    if not is_starosta:
        await callback.answer(
            "Только староста группы или администратор может добавлять экзамены!" if lang == "ru" else "Only starosta can manage exams!",
            show_alert=True
        )
        return

    if callback_data.action == "add":
        await state.set_state(ExamStates.waiting_for_subject)
        await callback.message.answer(
            "📖 Введите <b>название предмета</b> экзамена / зачета (например: <i>Математика</i>):",
            parse_mode="HTML"
        )
        await callback.answer()
    elif callback_data.action == "del_pick":
        exams = await database.get_group_exams(group_id)
        if not exams:
            await callback.answer("Нет экзаменов для удаления.", show_alert=True)
            return
        kb = get_exams_delete_keyboard(exams=exams, lang=lang)
        await callback.message.edit_text(
            "Выберите экзамен/зачет для удаления:" if lang == "ru" else "Select exam to delete:",
            reply_markup=kb,
            parse_mode="HTML"
        )
        await callback.answer()
    elif callback_data.action == "del":
        await database.delete_group_exam(callback_data.exam_id, group_id)
        await callback.answer(get_text("exams_deleted", lang))
        exams = await database.get_group_exams(group_id)
        kb = get_exams_list_keyboard(exams=exams, is_starosta=is_starosta, lang=lang)
        group_name = user.get("group_name", "")
        title = get_text("exams_title", lang, group_name=group_name)
        text = title + ("\n".join([f"• <b>{e['subject']}</b> ({e['exam_date']})" for e in exams]) if exams else get_text("exams_empty", lang))
        await callback.message.edit_text(text, reply_markup=kb, parse_mode="HTML")


@router.message(ExamStates.waiting_for_subject)
async def process_exam_subject(message: Message, state: FSMContext):
    """Сохранение предмета и переход к дате экзамена."""
    subj = (message.text or "").strip()
    if not subj:
        await message.answer("⚠️ Введите название предмета:")
        return
    await state.update_data(subject=subj)
    await state.set_state(ExamStates.waiting_for_date)
    await message.answer(
        "📅 Введите <b>дату проведения</b> в формате <code>ДД.ММ.ГГГГ</code> (например, <code>25.12.2026</code>):",
        parse_mode="HTML"
    )


@router.message(ExamStates.waiting_for_date)
async def process_exam_date(message: Message, state: FSMContext):
    """Сохранение даты и переход к времени."""
    dt_str = (message.text or "").strip()
    await state.update_data(exam_date=dt_str)
    await state.set_state(ExamStates.waiting_for_time)
    await message.answer(
        "🕒 Введите <b>время начала</b> (например, <code>09:00</code>) или отправьте <code>-</code> (прочерк), если время не известно:",
        parse_mode="HTML"
    )


@router.message(ExamStates.waiting_for_time)
async def process_exam_time(message: Message, state: FSMContext):
    """Сохранение времени и переход к аудитории."""
    tm_str = (message.text or "").strip()
    if tm_str == "-":
        tm_str = ""
    await state.update_data(exam_time=tm_str)
    await state.set_state(ExamStates.waiting_for_room)
    await message.answer(
        "🏢 Введите <b>номер аудитории</b> (например, <code>304</code>) или <code>-</code> (прочерк):",
        parse_mode="HTML"
    )


@router.message(ExamStates.waiting_for_room)
async def process_exam_room(message: Message, state: FSMContext):
    """Сохранение кабинета и переход к преподавателю."""
    rm_str = (message.text or "").strip()
    if rm_str == "-":
        rm_str = ""
    await state.update_data(room=rm_str)
    await state.set_state(ExamStates.waiting_for_teacher)
    await message.answer(
        "👨‍🏫 Введите <b>ФИО преподавателя</b> (например, <i>Иванов И.И.</i>) или <code>-</code> (прочерк):",
        parse_mode="HTML"
    )


@router.message(ExamStates.waiting_for_teacher)
async def process_exam_teacher(message: Message, state: FSMContext, database: Database = default_db):
    """Завершение добавления экзамена."""
    tchr_str = (message.text or "").strip()
    if tchr_str == "-":
        tchr_str = ""
    data = await state.get_data()
    await state.clear()

    user = await database.get_user(message.from_user.id)
    group_id = user["group_id"]
    lang = user.get("language", "ru") if user else "ru"

    subj = data.get("subject", "")
    e_date = data.get("exam_date", "")
    e_time = data.get("exam_time", "")
    e_room = data.get("room", "")

    await database.add_group_exam(
        group_id=group_id,
        subject=subj,
        exam_date=e_date,
        exam_time=e_time,
        room=e_room,
        teacher=tchr_str,
        author_id=message.from_user.id,
    )

    exams = await database.get_group_exams(group_id)
    kb = get_exams_list_keyboard(exams=exams, is_starosta=True, lang=lang)
    confirm_text = get_text("exams_saved", lang, subject=subj)
    await message.answer(
        f"{confirm_text}\n\n📅 Дата: <b>{e_date}</b> {e_time}\n🏢 Кабинет: <b>{e_room or '—'}</b>\n👨‍🏫 Преподаватель: <b>{tchr_str or '—'}</b>",
        reply_markup=kb,
        parse_mode="HTML"
    )


# -------------------------------------------------------------------------
# ЛИЧНЫЕ ЗАМЕТКИ / ДЕДЛАЙНЫ
# -------------------------------------------------------------------------

def _format_notes_text(notes: list[dict[str, Any]], lang: str = "ru") -> str:
    title = get_text("notes_title", lang)
    if not notes:
        return title + get_text("notes_empty", lang)
    lines = [title]
    for idx, nt in enumerate(notes, start=1):
        t = nt["note_text"]
        t_date = nt.get("target_date")
        t_pair = nt.get("target_pair")
        extra = []
        if t_date:
            try:
                p_dt = datetime.strptime(t_date, "%Y-%m-%d")
                extra.append(f"🗓 {p_dt.day} {MONTHS_RU.get(p_dt.month, '')}")
            except Exception:
                extra.append(f"🗓 {t_date}")
        if t_pair is not None and t_pair > 0:
            extra.append(f"⏰ {t_pair}-я пара")
        elif t_pair == 0:
            extra.append("⏰ к началу дня")
        extra_str = f" <i>({', '.join(extra)})</i>" if extra else ""
        lines.append(f"{idx}. 📌 <b>{t}</b>{extra_str}\n")
    return "\n".join(lines)


def _parse_input_date(text: str) -> Optional[str]:
    """Парсит введенную дату в формат YYYY-MM-DD."""
    text = text.strip().lower()
    if not text or text in ("none", "нет", "пропустить", "без даты", "-"):
        return None
    today = date.today()
    for fmt in ("%d.%m.%Y", "%d.%m", "%d-%m-%Y", "%d-%m", "%Y-%m-%d"):
        try:
            dt = datetime.strptime(text, fmt)
            year = dt.year if fmt in ("%d.%m.%Y", "%d-%m-%Y", "%Y-%m-%d") else today.year
            res = date(year, dt.month, dt.day)
            return res.isoformat()
        except ValueError:
            pass
    return None


@router.message(Command("notes"))
@router.message(F.text.in_({"📝 Личные заметки", "📝 Personal notes", "📝 Notes"}))
async def cmd_notes(message: Message, database: Database = default_db):
    """Персональный блокнот студента с личными дедлайнами и долгами."""
    user_id = message.from_user.id
    user = await database.get_user(user_id)
    lang = user.get("language", "ru") if user else "ru"

    notes = await database.get_user_notes(user_id)
    text = _format_notes_text(notes, lang)
    kb = get_user_notes_keyboard(notes=notes, lang=lang)
    await message.answer(text, reply_markup=kb, parse_mode="HTML")


@router.callback_query(NotesCallback.filter())
async def cb_notes(
    callback: CallbackQuery, callback_data: NotesCallback, state: FSMContext, database: Database = default_db
):
    """Управление личными заметками."""
    user_id = callback.from_user.id
    user = await database.get_user(user_id)
    lang = user.get("language", "ru") if user else "ru"

    if callback_data.action == "cancel":
        await state.clear()
        notes = await database.get_user_notes(user_id)
        kb = get_user_notes_keyboard(notes=notes, lang=lang)
        text = _format_notes_text(notes, lang)
        await callback.message.edit_text(text, reply_markup=kb, parse_mode="HTML")
        await callback.answer()
        return

    if callback_data.action == "add":
        await state.set_state(NoteStates.waiting_for_text)
        await callback.message.answer(get_text("notes_prompt_text", lang), parse_mode="HTML")
        await callback.answer()
    elif callback_data.action == "del_pick":
        notes = await database.get_user_notes(user_id)
        if not notes:
            await callback.answer("У вас нет заметок для удаления.", show_alert=True)
            return
        kb = get_user_notes_delete_keyboard(notes=notes, lang=lang)
        await callback.message.edit_text(
            "Выберите заметку для удаления:" if lang == "ru" else "Select note to delete:",
            reply_markup=kb,
            parse_mode="HTML"
        )
        await callback.answer()
    elif callback_data.action == "del":
        await database.delete_user_note(callback_data.note_id, user_id)
        await callback.answer(get_text("notes_deleted", lang))
        notes = await database.get_user_notes(user_id)
        kb = get_user_notes_keyboard(notes=notes, lang=lang)
        text = _format_notes_text(notes, lang)
        await callback.message.edit_text(text, reply_markup=kb, parse_mode="HTML")


@router.message(NoteStates.waiting_for_text)
async def process_note_text(message: Message, state: FSMContext, database: Database = default_db):
    """Шаг 1 создания заметки: сохранение текста и запрос дедлайна."""
    note_text = (message.text or "").strip()
    if not note_text:
        await message.answer("⚠️ Текст заметки не может быть пустым. Введите текст:")
        return

    user_id = message.from_user.id
    user = await database.get_user(user_id)
    lang = user.get("language", "ru") if user else "ru"

    await state.update_data(note_text=note_text)
    await state.set_state(NoteStates.waiting_for_date)
    kb = get_note_date_quick_keyboard(lang=lang)
    prompt = (
        "📅 <b>К какому числу нужно выполнить задание?</b>\n\n"
        "Выберите дату кнопкой ниже или введите дату сообщением в формате <code>ДД.ММ</code> (например, <code>20.09</code>):"
        if lang == "ru"
        else (
            "📅 <b>What date is this note for?</b>\n\n"
            "Choose a date below or enter <code>DD.MM</code> (e.g. <code>20.09</code>):"
        )
    )
    await message.answer(prompt, reply_markup=kb, parse_mode="HTML")


@router.callback_query(NoteDateCallback.filter())
async def cb_note_date(callback: CallbackQuery, callback_data: NoteDateCallback, state: FSMContext, database: Database = default_db):
    """Выбор даты дедлайна инлайн-кнопкой."""
    user_id = callback.from_user.id
    user = await database.get_user(user_id)
    lang = user.get("language", "ru") if user else "ru"

    target_date = None if callback_data.date_str == "none" else callback_data.date_str
    await state.update_data(target_date=target_date)
    await state.set_state(NoteStates.waiting_for_pair)

    kb = get_note_pair_quick_keyboard(lang=lang)
    prompt = (
        "⏰ <b>К какой паре нужно подготовить?</b>\n\n"
        "Выберите пару, чтобы бот напомнил вам вовремя:"
        if lang == "ru"
        else "⏰ <b>For which class?</b>\n\nChoose a class so the bot can remind you on time:"
    )
    await callback.message.edit_text(prompt, reply_markup=kb, parse_mode="HTML")
    await callback.answer()


@router.message(NoteStates.waiting_for_date)
async def process_note_date_msg(message: Message, state: FSMContext, database: Database = default_db):
    """Ввод даты текстом."""
    user_id = message.from_user.id
    user = await database.get_user(user_id)
    lang = user.get("language", "ru") if user else "ru"

    text = (message.text or "").strip()
    target_date = _parse_input_date(text)

    await state.update_data(target_date=target_date)
    await state.set_state(NoteStates.waiting_for_pair)

    kb = get_note_pair_quick_keyboard(lang=lang)
    prompt = (
        "⏰ <b>К какой паре нужно подготовить?</b>\n\n"
        "Выберите пару, чтобы бот напомнил вам вовремя:"
        if lang == "ru"
        else "⏰ <b>For which class?</b>\n\nChoose a class for reminder:"
    )
    await message.answer(prompt, reply_markup=kb, parse_mode="HTML")


@router.callback_query(NotePairCallback.filter())
async def cb_note_pair(callback: CallbackQuery, callback_data: NotePairCallback, state: FSMContext, database: Database = default_db):
    """Выбор пары и сохранение заметки."""
    data = await state.get_data()
    await state.clear()

    user_id = callback.from_user.id
    user = await database.get_user(user_id)
    lang = user.get("language", "ru") if user else "ru"

    note_text = data.get("note_text", "")
    target_date = data.get("target_date")
    pair_val = callback_data.pair
    target_pair = pair_val if pair_val >= 0 else None

    await database.add_user_note(user_id, note_text, target_date=target_date, target_pair=target_pair)
    await callback.answer("Заметка сохранена!")

    notes = await database.get_user_notes(user_id)
    kb = get_user_notes_keyboard(notes=notes, lang=lang)

    deadline_parts = []
    if target_date:
        try:
            dt = datetime.strptime(target_date, "%Y-%m-%d")
            deadline_parts.append(f"🗓 {dt.day} {MONTHS_RU.get(dt.month, '')}")
        except Exception:
            deadline_parts.append(f"🗓 {target_date}")
    if target_pair is not None and target_pair > 0:
        deadline_parts.append(f"⏰ {target_pair}-я пара")
    elif target_pair == 0:
        deadline_parts.append("⏰ К началу дня")

    deadline_str = f"\n🗓 <b>Срок сдачи:</b> {', '.join(deadline_parts)}\n⏰ <i>Бот пришлет вам напоминание перед этой парой!</i>" if deadline_parts else ""

    text = (
        f"✅ <b>Заметка успешно сохранена в вашем блокноте!</b>\n\n"
        f"📌 <b>Заметка:</b> {note_text}"
        f"{deadline_str}"
    )
    await callback.message.edit_text(text, reply_markup=kb, parse_mode="HTML")


# Меню "Настройки и связь"
@router.message(F.text.in_({"⚙️ Настройки и связь", "⚙️ Settings & info", "⚙️ Настройки"}), StateFilter("*"))
async def cmd_settings_info_menu(message: Message, state: Optional[FSMContext] = None, database: Database = default_db):
    """Отдельное меню настроек языка, добавления бота в группу, связи с автором и руководства."""
    if state:
        await state.clear()
    user = await database.get_user(message.from_user.id)
    lang = user.get("language", "ru") if user else "ru"
    text = (
        "⚙️ <b>Настройки и связь с автором</b>\n\n"
        "Выберите интересующий вас раздел с помощью кнопок ниже:\n"
        "• <b>👥 Моя подгруппа:</b> выбор 1-й / 2-й подгруппы для фильтрации пар\n"
        "• <b>⚙️ Сменить группу:</b> выбор другой специальности и группы\n"
        "• <b>🌐 Язык:</b> сменить язык интерфейса бота (RU / EN)\n"
        "• <b>➕ Бот в группу:</b> добавить бота в беседу вашей группы\n"
        "• <b>👨‍💻 Связь с автором:</b> написать разработчику или сообщить об ошибке\n"
        "• <b>📖 Инструкция:</b> руководство пользователя по возможностям бота"
        if lang == "ru"
        else (
            "⚙️ <b>Settings & Info</b>\n\n"
            "Choose a section below:\n"
            "• <b>👥 My subgroup:</b> choose 1st / 2nd subgroup\n"
            "• <b>⚙️ Change group:</b> select a different specialty and group\n"
            "• <b>🌐 Language:</b> change bot language\n"
            "• <b>➕ Bot to group:</b> add bot to your group chat\n"
            "• <b>👨‍💻 Contact author:</b> developer contacts and feedback\n"
            "• <b>📖 Guide:</b> user manual for bot features"
        )
    )
    kb = get_settings_info_keyboard(lang=lang)
    await message.answer(text, reply_markup=kb, parse_mode="HTML")


# -------------------------------------------------------------------------
# НАСТРОЙКИ УВЕДОМЛЕНИЙ И РЕГУЛИРОВКА ВРЕМЕНИ
# -------------------------------------------------------------------------

@router.callback_query(NotificationSettingCallback.filter())
async def cb_notification_settings(
    callback: CallbackQuery, callback_data: NotificationSettingCallback, database: Database = default_db
):
    """Обработка настройки времени отправки расписания и напоминаний."""
    chat_id = callback.message.chat.id
    thread_id = getattr(callback.message, "message_thread_id", None)
    is_group = callback.message.chat.type in ("group", "supergroup")
    user_id = callback.from_user.id

    if is_group:
        if not await is_group_admin(callback.bot, chat_id, user_id):
            await callback.answer(
                "Только администраторы группы могут изменять настройки рассылок!",
                show_alert=True,
            )
            return
        chat = await database.get_chat(chat_id, thread_id)
        if not chat:
            await callback.answer("Чат не настроен.", show_alert=True)
            return
        evening_time = chat.get("evening_notify_time", "20:00") or "20:00"
        morning_time = chat.get("morning_notify_time", "08:00") or "08:00"
        lead_min = int(chat.get("notify_lead_minutes") or 0)
        enabled = bool(chat.get("notifications_enabled", 1))
        lang = "ru"
    else:
        user = await database.get_user(user_id)
        if not user:
            await callback.answer("Пользователь не найден.", show_alert=True)
            return
        evening_time = user.get("evening_notify_time", "20:00") or "20:00"
        morning_time = user.get("morning_notify_time", "08:00") or "08:00"
        lead_min = int(user.get("notify_lead_minutes") or 0)
        enabled = bool(user.get("notifications_enabled", 1))
        lang = user.get("language", "ru")

    target = callback_data.target
    val = callback_data.value

    if target == "toggle":
        new_enabled = not enabled
        if is_group:
            await database.set_chat_notifications(chat_id, thread_id, new_enabled)
        else:
            await database.set_user_notifications(user_id, new_enabled)
        enabled = new_enabled
        await callback.answer("Статус рассылок изменен!" if lang == "ru" else "Notification status toggled!")
    elif target == "evening_menu":
        kb = get_evening_time_selection_keyboard(evening_time, lang)
        msg_text = (
            "🌙 <b>Выберите время вечерней рассылки расписания на завтра:</b>\n\n"
            f"Текущее время: <b>{evening_time}</b>"
            if lang == "ru"
            else f"🌙 <b>Select evening dispatch time for tomorrow's schedule:</b>\n\nCurrent: <b>{evening_time}</b>"
        )
        await callback.message.edit_text(msg_text, reply_markup=kb, parse_mode="HTML")
        await callback.answer()
        return
    elif target == "evening_set":
        evening_time = val
        if is_group:
            await database.set_chat_evening_time(chat_id, thread_id, evening_time)
        else:
            await database.set_user_evening_time(user_id, evening_time)
        await callback.answer(f"Вечерняя рассылка: {evening_time}" if lang == "ru" else f"Evening time: {evening_time}")
    elif target == "morning_menu":
        kb = get_morning_time_selection_keyboard(morning_time, lang)
        msg_text = (
            "☀️ <b>Выберите время утренней рассылки расписания на сегодня:</b>\n\n"
            f"Текущее время: <b>{morning_time}</b>"
            if lang == "ru"
            else f"☀️ <b>Select morning dispatch time for today's schedule:</b>\n\nCurrent: <b>{morning_time}</b>"
        )
        await callback.message.edit_text(msg_text, reply_markup=kb, parse_mode="HTML")
        await callback.answer()
        return
    elif target == "morning_set":
        morning_time = val
        if is_group:
            await database.set_chat_morning_time(chat_id, thread_id, morning_time)
        else:
            await database.set_user_morning_time(user_id, morning_time)
        await callback.answer(f"Утренняя рассылка: {morning_time}" if lang == "ru" else f"Morning time: {morning_time}")
    elif target == "lead_menu":
        kb = get_notification_lead_time_keyboard(lang)
        msg_text = (
            "⏱ <b>Выберите время напоминания перед началом пары:</b>"
            if lang == "ru"
            else "⏱ <b>Choose reminder time before classes:</b>"
        )
        await callback.message.edit_text(msg_text, reply_markup=kb, parse_mode="HTML")
        await callback.answer()
        return

    # Отображаем основное меню настроек
    title = get_text("notif_settings_title", lang)
    kb = get_notification_settings_keyboard(
        evening_time=evening_time,
        morning_time=morning_time,
        lead_min=lead_min,
        enabled=enabled,
        lang=lang,
    )
    try:
        await callback.message.edit_text(title, reply_markup=kb, parse_mode="HTML")
    except Exception:
        pass


# -------------------------------------------------------------------------
# ПРОПУСК ПАР / СОН И ВЫБОР ПОДГРУППЫ
# -------------------------------------------------------------------------

@router.message(F.text.in_({"💤 Не иду на пару", "💤 Skip pair / Sleep", "💤 Пропуск пар"}))
@router.message(Command("skip_pair"))
@router.message(Command("sleep"))
async def handle_skip_pair_menu(message: Message, database: Database = default_db):
    """
    Интерактивное управление посещаемостью: пропуск конкретных пар или режим «Сплю до 2-й пары».
    """
    user = await _get_authorized_user(message, database)
    if not user:
        return

    now_dt = get_current_college_time()
    today = now_dt.date()
    group_id = user["group_id"]
    group_url = user["group_url"]
    lang = user.get("language", "ru")
    user_id = message.from_user.id

    wait_msg = await message.answer("⏳ <i>Загружаю данные о парах...</i>", parse_mode="HTML")
    try:
        today_sched = await timetable_parser.fetch_day_schedule(
            group_id=group_id, group_url=group_url, target_date=today
        )
        lessons = today_sched.get("lessons", [])

        # Если пары на сегодня уже кончились или их нет вовсе, переключаемся на завтра
        target_date = today
        if are_today_pairs_finished(lessons, now_dt) or not lessons:
            target_date = today + timedelta(days=1)
            tomorrow_sched = await timetable_parser.fetch_day_schedule(
                group_id=group_id, group_url=group_url, target_date=target_date
            )
            lessons = tomorrow_sched.get("lessons", [])

        date_str = target_date.isoformat()
        date_display = target_date.strftime("%d.%m.%Y")

        user_subgroup = user.get("subgroup", 0)
        overrides = user.get("subgroup_overrides") or {}
        filtered_lessons = filter_lessons_by_subgroup(lessons, user_subgroup, overrides)

        if not filtered_lessons:
            await wait_msg.edit_text(
                f"🎉 <b>На {date_display} пар нет!</b>\n\nМожно спокойно спать и отдыхать.",
                parse_mode="HTML"
            )
            return

        skipped_pairs = await database.get_skipped_pairs(user_id, date_str)
        kb = get_skip_pair_keyboard(filtered_lessons, skipped_pairs, date_str, lang=lang)

        msg_text = (
            f"💤 <b>Настройка пропуска пар / Сон</b>\n\n"
            f"📅 Дата: <b>{date_display}</b>\n\n"
            "Нажмите на пару, чтобы отметить её как пропущенную (или вернуть).\n"
            "• <i>Бот не будет присылать напоминания по пропущенным парам.</i>\n"
            "• <i>Статус автоматически отображается в виджете «📍 Где сейчас пара?».</i>"
            if lang == "ru"
            else (
                f"💤 <b>Skip Pairs / Sleep Mode</b>\n\n"
                f"📅 Date: <b>{date_display}</b>\n\n"
                "Tap on a class to toggle skip/attendance.\n"
                "• <i>The bot won't send alerts for skipped classes.</i>\n"
                "• <i>Status is updated in «📍 Where is pair now?».</i>"
            )
        )
        await wait_msg.edit_text(msg_text, reply_markup=kb, parse_mode="HTML")
    except Exception as e:
        logger.error("Ошибка открытия меню пропуска пар: %s", e, exc_info=True)
        await wait_msg.edit_text("❌ Не удалось загрузить данные. Пожалуйста, попробуйте позже.")


@router.callback_query(SkipPairCallback.filter())
async def cb_skip_pair(
    callback: CallbackQuery, callback_data: SkipPairCallback, database: Database = default_db
):
    action = callback_data.action
    user_id = callback.from_user.id
    date_str = callback_data.date_str

    if action == "close":
        try:
            await callback.message.delete()
        except Exception:
            await callback.message.edit_reply_markup(reply_markup=None)
        await callback.answer()
        return

    user = await database.get_user(user_id)
    if not user:
        await callback.answer("Ошибка: пользователь не найден")
        return

    group_id = user["group_id"]
    group_url = user["group_url"]
    lang = user.get("language", "ru")

    try:
        target_date = datetime.strptime(date_str, "%Y-%m-%d").date()
    except Exception:
        target_date = get_current_college_time().date()
        date_str = target_date.isoformat()

    if action == "toggle":
        p_num = callback_data.pair_number
        is_skipped = await database.toggle_skipped_pair(user_id, date_str, p_num)
        month_str = date_str[:7]
        m_stats = await database.get_monthly_skipped_stats(user_id, month_str)
        m_total = m_stats.get("total_skipped_pairs", 0)
        status_msg = f"Пара {p_num}: " + (
            f"пропуск 💤 (За месяц: {m_total})" if is_skipped else f"иду ✅ (За месяц: {m_total})"
        )
        await callback.answer(status_msg)
    elif action == "sleep_first":
        p_num = callback_data.pair_number or 1
        is_skipped = await database.toggle_skipped_pair(user_id, date_str, p_num)
        month_str = date_str[:7]
        m_stats = await database.get_monthly_skipped_stats(user_id, month_str)
        m_total = m_stats.get("total_skipped_pairs", 0)
        status_msg = (
            f"«Сплю до 2-й пары» 😴 (За месяц: {m_total})" if is_skipped else f"«Сплю» выключен ⏰ (За месяц: {m_total})"
        )
        await callback.answer(status_msg)
    elif action == "clear":
        await database.clear_skipped_pairs(user_id, date_str)
        month_str = date_str[:7]
        m_stats = await database.get_monthly_skipped_stats(user_id, month_str)
        m_total = m_stats.get("total_skipped_pairs", 0)
        await callback.answer(f"Все пары отмечены как посещаемые ✅ (За месяц: {m_total})")

    # Перерисовываем клавиатуру
    sched = await timetable_parser.fetch_day_schedule(
        group_id=group_id, group_url=group_url, target_date=target_date
    )
    lessons = sched.get("lessons", [])
    user_subgroup = user.get("subgroup", 0)
    overrides = user.get("subgroup_overrides") or {}
    filtered_lessons = filter_lessons_by_subgroup(lessons, user_subgroup, overrides)
    skipped_pairs = await database.get_skipped_pairs(user_id, date_str)

    kb = get_skip_pair_keyboard(filtered_lessons, skipped_pairs, date_str, lang=lang)
    try:
        await callback.message.edit_reply_markup(reply_markup=kb)
    except Exception:
        pass


@router.message(F.text.in_({"👥 Моя подгруппа", "👥 My subgroup"}))
@router.message(Command("subgroup"))
async def handle_subgroup_menu(message: Message, database: Database = default_db):
    """
    Выбор подгруппы пользователя (1, 2 или обе).
    """
    user = await _get_authorized_user(message, database)
    if not user:
        return

    current_sub = user.get("subgroup", 0)
    lang = user.get("language", "ru")
    kb = get_subgroup_selection_keyboard(current_sub, lang=lang)

    text = (
        "👥 <b>Выбор вашей учебной подгруппы</b>\n\n"
        "Укажите подгруппу, в которой вы учитесь, чтобы бот фильтровал пары:\n\n"
        "• <b>1-я подгруппа</b> — отображать только занятия 1-й подгруппы и общие пары\n"
        "• <b>2-я подгруппа</b> — отображать только занятия 2-й подгруппы и общие пары\n"
        "• <b>Вся группа (обе)</b> — показывать все пары без разделения"
        if lang == "ru"
        else (
            "👥 <b>Select your study subgroup</b>\n\n"
            "Choose your subgroup to filter schedule and reminders:\n\n"
            "• <b>1st subgroup</b> — show only 1st subgroup and common classes\n"
            "• <b>2nd subgroup</b> — show only 2nd subgroup and common classes\n"
            "• <b>Entire group (both)</b> — show all classes"
        )
    )
    await message.answer(text, reply_markup=kb, parse_mode="HTML")


@router.callback_query(SubgroupCallback.filter())
async def cb_subgroup(
    callback: CallbackQuery, callback_data: SubgroupCallback, database: Database = default_db
):
    action = callback_data.action
    if action == "close":
        try:
            await callback.message.delete()
        except Exception:
            await callback.message.edit_reply_markup(reply_markup=None)
        await callback.answer()
        return

    new_sub = callback_data.subgroup
    user_id = callback.from_user.id
    user = await database.get_user(user_id)
    lang = user.get("language", "ru") if user else "ru"

    await database.set_user_subgroup(user_id, new_sub)

    sub_label = (
        "1-я подгруппа" if new_sub == 1 else ("2-я подгруппа" if new_sub == 2 else "Вся группа (обе)")
    ) if lang == "ru" else (
        "1st subgroup" if new_sub == 1 else ("2nd subgroup" if new_sub == 2 else "Entire group (both)")
    )

    kb = get_subgroup_selection_keyboard(new_sub, lang=lang)
    text = (
        f"✅ <b>Подгруппа успешно сохранена: {sub_label}!</b>\n\n"
        "Теперь в расписании и напоминаниях перед парами будут учитываться только ваши пары."
        if lang == "ru"
        else (
            f"✅ <b>Subgroup saved: {sub_label}!</b>\n\n"
            "Schedule and class notifications will now be tailored to your subgroup."
        )
    )
    try:
        await callback.message.edit_text(text, reply_markup=kb, parse_mode="HTML")
    except Exception:
        pass
    await callback.answer(f"Сохранено: {sub_label}")


# -------------------------------------------------------------------------
# TELEGRAM MINI APP (WEBAPP)
# -------------------------------------------------------------------------

@router.message(Command("app"))
@router.message(Command("webapp"))
@router.message(F.text.in_({"📱 Приложение", "📱 Веб-расписание", "📱 WebApp", "📱 Открыть в приложении"}))
async def handle_open_webapp(message: Message, database: Database = default_db):
    """
    Открывает интерактивный Telegram Mini App (WebApp) с расписанием группы.
    """
    user = await _get_authorized_user(message, database)
    if not user:
        return

    user_id = message.from_user.id if message.from_user else 0
    group_id = user["group_id"]
    group_name = user["group_name"]
    group_url = user["group_url"]
    user_sub = user.get("subgroup", 0)
    lang = user.get("language", "ru")

    today = get_current_college_time().date()
    week_schedule = []
    try:
        week_schedule = await timetable_parser.fetch_week_schedule(
            group_id=group_id, group_url=group_url, start_date=today
        )
    except Exception as e:
        logger.warning("Не удалось предзагрузить неделю для WebApp: %s", e)

    # Загружаем пропуски на текущую неделю для мгновенной синхронизации в WebApp
    skipped_list = []
    if week_schedule:
        for d in week_schedule:
            d_date = d.get("date")
            if d_date:
                u_skips = await database.get_skipped_pairs(user_id, d_date)
                for p_num in u_skips:
                    skipped_list.append(f"{d_date}:{p_num}")

    webapp_url = build_webapp_url(
        group_name=group_name,
        subgroup=user_sub,
        week_days=week_schedule if week_schedule else None,
        user_id=user_id,
        skipped_pairs=skipped_list if skipped_list else None,
    )

    kb = get_webapp_inline_keyboard(webapp_url, lang=lang)
    text = (
        f"📱 <b>Интерактивное расписание группы {group_name}</b>\n\n"
        "Нажмите кнопку ниже, чтобы открыть полноэкранный Mini App прямо в Telegram:\n"
        "• ⚡ Мгновенное переключение дней недели и свайпы\n"
        "• 💡 Информация о парах по подгруппам\n"
        "• 🟢 Live-таймер текущей пары и перемены\n"
        "• 💤 Отметка пропуска пар в один клик и учет за месяц\n"
        "• 🔍 Быстрый поиск аудиторий и преподавателей"
        if lang == "ru"
        else (
            f"📱 <b>Interactive Schedule for {group_name}</b>\n\n"
            "Tap the button below to launch Telegram Mini App:\n"
            "• ⚡ Instant day switching and gestures\n"
            "• 💡 Subgroup classes info banner\n"
            "• 🟢 Live class and break countdowns\n"
            "• 💤 Toggle sleep/skip classes and monthly tracking\n"
            "• 🔍 Fast room and teacher search"
        )
    )
    await message.answer(text, reply_markup=kb, parse_mode="HTML")


@router.message(F.text.in_({"📊 Мои пропуски", "📊 My skips", "📊 Пропуски", "📊 Пропуски за месяц"}))
@router.message(Command("skips", "attendance"))
async def handle_skips_stats(message: Message, database: Database = default_db):
    """
    Показывает статистику пропущенных пар студента за текущий месяц (и сравнение с предыдущим).
    """
    user = await _get_authorized_user(message, database)
    if not user:
        return

    user_id = message.from_user.id
    lang = user.get("language", "ru")
    now = get_current_college_time()
    current_month_str = now.strftime("%Y-%m")

    month_names_ru = {
        1: "Январь", 2: "Февраль", 3: "Март", 4: "Апрель", 5: "Май", 6: "Июнь",
        7: "Июль", 8: "Август", 9: "Сентябрь", 10: "Октябрь", 11: "Ноябрь", 12: "Декабрь"
    }
    month_name_ru = month_names_ru.get(now.month, now.strftime("%B"))

    stats = await database.get_monthly_skipped_stats(user_id, current_month_str)
    total_skipped = stats.get("total_skipped_pairs", 0)
    total_hours = stats.get("total_academic_hours", 0)
    days_count = stats.get("days_count", 0)
    days_detail = stats.get("days_detail", [])

    # Предыдущий месяц для сравнения
    prev_month_date = now.replace(day=1) - timedelta(days=1)
    prev_month_str = prev_month_date.strftime("%Y-%m")
    prev_month_name = month_names_ru.get(prev_month_date.month, prev_month_date.strftime("%B"))
    prev_stats = await database.get_monthly_skipped_stats(user_id, prev_month_str)
    prev_total = prev_stats.get("total_skipped_pairs", 0)

    if lang == "ru":
        if total_skipped == 0:
            text = (
                f"🎉 <b>Отличная посещаемость!</b>\n\n"
                f"🗓 Месяц: <b>{month_name_ru} {now.year}</b>\n"
                f"💤 Вы не пропустили ни одной пары (100% посещаемость)!\n\n"
                f"<i>В прошлом месяце ({prev_month_name}): {prev_total} пропусков.</i>\n"
                "Так держать, отличный результат! 🎓"
            )
        else:
            detail_lines = []
            for d in days_detail:
                try:
                    d_dt = datetime.strptime(d["date"], "%Y-%m-%d")
                    d_fmt = d_dt.strftime("%d.%m")
                except Exception:
                    d_fmt = d["date"]
                pairs_str = ", ".join(f"№{p}" for p in d["pair_numbers"])
                detail_lines.append(f"• <b>{d_fmt}</b>: пара {pairs_str}")

            detail_text = "\n".join(detail_lines)
            text = (
                f"📊 <b>Статистика пропусков за {month_name_ru} {now.year}</b>\n\n"
                f"💤 <b>Всего пропущено пар:</b> {total_skipped} ({total_hours} акад. ч.)\n"
                f"📅 <b>Дней с пропусками:</b> {days_count}\n\n"
                f"<b>Детализация по дням:</b>\n"
                f"{detail_text}\n\n"
                f"<i>В прошлом месяце ({prev_month_name}): {prev_total} пропусков.</i>"
            )
    else:
        month_name_en = now.strftime("%B")
        prev_month_name_en = prev_month_date.strftime("%B")
        if total_skipped == 0:
            text = (
                f"🎉 <b>Perfect Attendance!</b>\n\n"
                f"🗓 Month: <b>{month_name_en} {now.year}</b>\n"
                f"💤 You haven't skipped any classes (100% attendance)!\n\n"
                f"<i>Last month ({prev_month_name_en}): {prev_total} skips.</i>\n"
                "Keep up the great work! 🎓"
            )
        else:
            detail_lines = []
            for d in days_detail:
                pairs_str = ", ".join(f"#{p}" for p in d["pair_numbers"])
                detail_lines.append(f"• <b>{d['date']}</b>: class {pairs_str}")
            detail_text = "\n".join(detail_lines)
            text = (
                f"📊 <b>Skipped Classes Statistics ({month_name_en} {now.year})</b>\n\n"
                f"💤 <b>Total skipped classes:</b> {total_skipped} ({total_hours} acad. hrs)\n"
                f"📅 <b>Days with skips:</b> {days_count}\n\n"
                f"<b>Breakdown by date:</b>\n"
                f"{detail_text}\n\n"
                f"<i>Last month ({prev_month_name_en}): {prev_total} skips.</i>"
            )

    builder = InlineKeyboardBuilder()
    builder.row(
        InlineKeyboardButton(
            text="💤 Отметить / изменить пропуск" if lang == "ru" else "💤 Toggle Skip Class",
            callback_data="skip_date_today"
        )
    )
    await message.answer(text, reply_markup=builder.as_markup(), parse_mode="HTML")


@router.callback_query(F.data == "skip_date_today")
async def cb_skip_date_today(callback: CallbackQuery, database: Database = default_db):
    user_id = callback.from_user.id
    user = await database.get_user(user_id)
    if not user:
        await callback.answer("Ошибка: пользователь не найден")
        return

    today = get_current_college_time().date()
    target_date = today
    if today.weekday() == 6:
        target_date = today + timedelta(days=1)

    group_id = user["group_id"]
    group_url = user["group_url"]
    lang = user.get("language", "ru")
    date_str = target_date.isoformat()
    date_display = target_date.strftime("%d.%m.%Y")

    sched = await timetable_parser.fetch_day_schedule(
        group_id=group_id, group_url=group_url, target_date=target_date
    )
    lessons = sched.get("lessons", [])
    user_subgroup = user.get("subgroup", 0)
    overrides = user.get("subgroup_overrides") or {}
    filtered_lessons = filter_lessons_by_subgroup(lessons, user_subgroup, overrides)
    skipped_pairs = await database.get_skipped_pairs(user_id, date_str)
    kb = get_skip_pair_keyboard(filtered_lessons, skipped_pairs, date_str, lang=lang)

    msg_text = (
        f"💤 <b>Настройка пропуска пар / Сон</b>\n\n"
        f"📅 Дата: <b>{date_display}</b>\n\n"
        "Нажмите на пару, чтобы отметить её как пропущенную (или вернуть).\n"
        "• <i>Бот не будет присылать напоминания по пропущенным парам.</i>\n"
        "• <i>В конце месяца бот пришлет сводную статистику посещаемости.</i>"
        if lang == "ru"
        else (
            f"💤 <b>Skip Pairs / Sleep Mode</b>\n\n"
            f"📅 Date: <b>{date_display}</b>\n\n"
            "Tap on a class to toggle skip/attendance.\n"
            "• <i>The bot won't send alerts for skipped classes.</i>\n"
            "• <i>Monthly attendance report will be sent at month end.</i>"
        )
    )
    try:
        await callback.message.edit_text(msg_text, reply_markup=kb, parse_mode="HTML")
    except Exception:
        await callback.message.answer(msg_text, reply_markup=kb, parse_mode="HTML")
    await callback.answer()
