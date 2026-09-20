from datetime import date, timedelta
from typing import Any, Optional
from aiogram.filters.callback_data import CallbackData
from aiogram.types import (
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    KeyboardButton,
    ReplyKeyboardMarkup,
    WebAppInfo,
)
from aiogram.utils.keyboard import InlineKeyboardBuilder, ReplyKeyboardBuilder


# -------------------------------------------------------------------------
# CallbackData фабрики
# (Telegram строго ограничивает callback_data 64 байтами, поэтому используем индексы/ID)
# -------------------------------------------------------------------------

class SpecialtyCallback(CallbackData, prefix="sp"):
    action: str  # "pick"
    idx: int     # Индекс специальности в списке


class SpecialtyPageCallback(CallbackData, prefix="spp"):
    page: int    # Номер страницы пагинации


class GroupCallback(CallbackData, prefix="grp"):
    action: str  # "pick"
    group_id: str  # id группы (например, '6g5eqnp6')


class NavigationCallback(CallbackData, prefix="nav"):
    to: str      # "specialties", "main_menu"


class ScheduleNavCallback(CallbackData, prefix="schnav"):
    action: str  # "today", "tomorrow", "today_past", "week"
    date_str: str  # YYYY-MM-DD


class AdminCallback(CallbackData, prefix="adm"):
    action: str  # "menu", "stats", "groups", "users", "broadcast", "warmup", "clearcache", "close"


class HelpCallback(CallbackData, prefix="hlp"):
    section: str  # "main", "student", "group", "commands"


class TeacherChoiceCallback(CallbackData, prefix="tchc"):
    action: str  # "fio", "subjects", "menu"


class SubjectCallback(CallbackData, prefix="sbj"):
    idx: int


class LanguageCallback(CallbackData, prefix="lng"):
    lang: str  # "ru", "en"


class LeadTimeCallback(CallbackData, prefix="ldt"):
    minutes: int  # 0, 5, 10, 15, 20, 30, 45


class StarostaCallback(CallbackData, prefix="star"):
    action: str  # "claim", "confirm_claim", "resign", "menu"


class HomeworkCallback(CallbackData, prefix="hw"):
    action: str  # "view", "add", "del_pick", "del", "cancel"
    hw_id: int = 0


class HwDateCallback(CallbackData, prefix="hwd"):
    date_str: str


class HwSubjectCallback(CallbackData, prefix="hwsbj"):
    idx: int


class AdminStarostaApproveCallback(CallbackData, prefix="stapp"):
    action: str  # "approve", "reject", "rem_confirm", "rem_cancel"
    req_id: int = 0
    group_id: str
    candidate_id: int


class BellsCallback(CallbackData, prefix="bel"):
    action: str  # "refresh", "menu"


class GroupChatLinkCallback(CallbackData, prefix="gcl"):
    action: str  # "set", "del", "menu"


class CampusCallback(CallbackData, prefix="cmp"):
    action: str  # "floor", "search", "menu"
    floor: int = 1


class MyTeacherCallback(CallbackData, prefix="myt"):
    idx: int


class ExamsCallback(CallbackData, prefix="exm"):
    action: str  # "menu", "add", "del_pick", "del", "cancel"
    exam_id: int = 0


class NotesCallback(CallbackData, prefix="nts"):
    action: str  # "menu", "add", "del_pick", "del", "cancel"
    note_id: int = 0


class NoteDateCallback(CallbackData, prefix="ntd"):
    date_str: str


class NotePairCallback(CallbackData, prefix="ntp"):
    pair: int


class NotificationSettingCallback(CallbackData, prefix="nset", sep="#"):
    target: str  # "menu", "toggle", "evening_menu", "evening_set", "morning_menu", "morning_set", "lead_menu", "lead_set"
    value: str = ""


class SubgroupCallback(CallbackData, prefix="subgrp"):
    action: str  # "set", "close"
    subgroup: int = 0  # 0 = обе, 1 = 1-я, 2 = 2-я


class SkipPairCallback(CallbackData, prefix="skppr"):
    action: str  # "toggle", "sleep_first", "clear", "close"
    pair_number: int = 0
    date_str: str = ""


# -------------------------------------------------------------------------
# Inline-клавиатуры для пошагового выбора
# -------------------------------------------------------------------------

def get_specialties_inline_keyboard(
    specialties: list[str], show_back_to_menu: bool = True
) -> InlineKeyboardMarkup:
    """
    Шаг 1: Inline-клавиатура со списком всех специальностей.
    Отображает все специальности без скрытия за кнопку «Вперед».
    Содержит кнопку «Назад в меню».
    """
    builder = InlineKeyboardBuilder()

    for idx, spec_name in enumerate(specialties):
        # Отображаем понятное название специальности
        button_text = spec_name if len(spec_name) <= 45 else spec_name[:42] + "..."
        builder.row(
            InlineKeyboardButton(
                text=button_text,
                callback_data=SpecialtyCallback(action="pick", idx=idx).pack()
            )
        )

    # Кнопка «Назад в меню»
    if show_back_to_menu:
        builder.row(
            InlineKeyboardButton(
                text="⬅️ Назад в главное меню",
                callback_data=NavigationCallback(to="main_menu").pack()
            )
        )

    return builder.as_markup()


def get_groups_inline_keyboard(groups: list[dict[str, str]]) -> InlineKeyboardMarkup:
    """
    Шаг 2: Inline-клавиатура со списком групп выбранной специальности.
    Располагает группы строго по 2 в ряд без лишних символов,
    чтобы названия групп гарантированно не обрезались на смартфонах.
    """
    builder = InlineKeyboardBuilder()

    # Сортируем группы по названию
    sorted_groups = sorted(groups, key=lambda g: g.get("group_name", ""))

    # Добавляем группы по 2 в ряд (оптимально для ширины экрана смартфона)
    row: list[InlineKeyboardButton] = []
    for g in sorted_groups:
        row.append(
            InlineKeyboardButton(
                text=g["group_name"],
                callback_data=GroupCallback(action="pick", group_id=g["id"]).pack()
            )
        )
        if len(row) == 2:
            builder.row(*row)
            row = []
    if row:
        builder.row(*row)

    # Кнопки возврата
    builder.row(
        InlineKeyboardButton(
            text="⬅️ Назад к специальностям",
            callback_data=NavigationCallback(to="specialties").pack()
        )
    )
    builder.row(
        InlineKeyboardButton(
            text="🏠 В главное меню",
            callback_data=NavigationCallback(to="main_menu").pack()
        )
    )

    return builder.as_markup()


RU_WEEKDAYS = {0: "Пн", 1: "Вт", 2: "Ср", 3: "Чт", 4: "Пт", 5: "Сб", 6: "Вс"}


def get_schedule_bonch_keyboard(
    target_date: date,
    show_back_to_menu: bool = True,
    show_today_past: bool = False,
    show_calendar: bool = False,
    webapp_url: Optional[str] = None,
) -> InlineKeyboardMarkup:
    """
    Интерактивная BonchGo-сетка навигации под сообщением расписания:
    Ряд 1: [ ⬅️ 11.09 Пт ]  [ 13.09 Вс ➡️ ]
    Ряд 2: [ ⏪ 05.09 Сб ]  [ 19.09 Сб ⏩ ]
    Ряд 3: [ 🖼 Картинка ]  [ 📆 Вся неделя ]
    Ряд 4: [ 📱 Открыть в приложении ] (опционально)
    Ряд 5: [ 📅 В календарь (.ics) ] (опционально)
    Ряд 6: [ 🏠 В главное меню ]
    """
    builder = InlineKeyboardBuilder()

    prev_day = target_date - timedelta(days=1)
    next_day = target_date + timedelta(days=1)
    prev_week = target_date - timedelta(days=7)
    next_week = target_date + timedelta(days=7)

    # Ряд 1: Перелистывание на 1 день вперед/назад
    btn_prev_day = f"⬅️ {prev_day.strftime('%d.%m')} {RU_WEEKDAYS[prev_day.weekday()]}"
    btn_next_day = f"{next_day.strftime('%d.%m')} {RU_WEEKDAYS[next_day.weekday()]} ➡️"
    builder.row(
        InlineKeyboardButton(
            text=btn_prev_day,
            callback_data=ScheduleNavCallback(action="day_to", date_str=prev_day.isoformat()).pack()
        ),
        InlineKeyboardButton(
            text=btn_next_day,
            callback_data=ScheduleNavCallback(action="day_to", date_str=next_day.isoformat()).pack()
        ),
    )

    # Ряд 2: Прыжок на неделю (-7 / +7 дней)
    btn_prev_week = f"⏪ {prev_week.strftime('%d.%m')} {RU_WEEKDAYS[prev_week.weekday()]}"
    btn_next_week = f"{next_week.strftime('%d.%m')} {RU_WEEKDAYS[next_week.weekday()]} ⏩"
    builder.row(
        InlineKeyboardButton(
            text=btn_prev_week,
            callback_data=ScheduleNavCallback(action="day_to", date_str=prev_week.isoformat()).pack()
        ),
        InlineKeyboardButton(
            text=btn_next_week,
            callback_data=ScheduleNavCallback(action="day_to", date_str=next_week.isoformat()).pack()
        ),
    )

    # Ряд 3: Вся неделя
    builder.row(
        InlineKeyboardButton(
            text="📆 Вся неделя",
            callback_data=ScheduleNavCallback(action="week", date_str=target_date.isoformat()).pack()
        ),
    )

    # Кнопка открытия в Telegram WebApp
    if webapp_url:
        builder.row(
            InlineKeyboardButton(
                text="📱 Открыть в приложении",
                web_app=WebAppInfo(url=webapp_url)
            )
        )

    # Дополнительная кнопка прошедших пар (если запрошено)
    if show_today_past:
        builder.row(
            InlineKeyboardButton(
                text="Показать прошедшее за сегодня",
                callback_data=ScheduleNavCallback(action="today_past", date_str=target_date.isoformat()).pack()
            )
        )

    # Ряд возврата в меню
    if show_back_to_menu:
        builder.row(
            InlineKeyboardButton(
                text="Назад в меню",
                callback_data=NavigationCallback(to="main_menu").pack()
            )
        )

    return builder.as_markup()


def get_schedule_nav_keyboard(
    current_date_str: str,
    show_today_past: bool = False,
    show_back_to_menu: bool = True,
) -> InlineKeyboardMarkup:
    """
    Совместимая функция навигации по расписанию, возвращающая BonchGo-раскладку.
    """
    try:
        cur_dt = date.fromisoformat(current_date_str)
    except Exception:
        cur_dt = date.today()
    return get_schedule_bonch_keyboard(
        target_date=cur_dt,
        show_back_to_menu=show_back_to_menu,
        show_today_past=show_today_past
    )


def get_help_inline_keyboard(
    current_section: str = "main", show_back_to_menu: bool = True
) -> InlineKeyboardMarkup:
    """
    Инлайн-клавиатура для интерактивного меню справки и инструкции.
    Позволяет переключаться между разделами:
    - Общая информация ('main')
    - Студенту в ЛС ('student')
    - Добавление в беседу ('group')
    - Список команд ('commands')
    """
    builder = InlineKeyboardBuilder()

    nav_buttons: list[InlineKeyboardButton] = []
    if current_section != "student":
        nav_buttons.append(
            InlineKeyboardButton(
                text="📱 Для студента",
                callback_data=HelpCallback(section="student").pack()
            )
        )
    if current_section != "group":
        nav_buttons.append(
            InlineKeyboardButton(
                text="👥 Добавить в беседу",
                callback_data=HelpCallback(section="group").pack()
            )
        )
    if current_section != "commands":
        nav_buttons.append(
            InlineKeyboardButton(
                text="📋 Все команды",
                callback_data=HelpCallback(section="commands").pack()
            )
        )
    if current_section != "main":
        nav_buttons.append(
            InlineKeyboardButton(
                text="📖 Обзор справки",
                callback_data=HelpCallback(section="main").pack()
            )
        )

    # Добавляем кнопки переключения секций по 2 в ряд
    for i in range(0, len(nav_buttons), 2):
        builder.row(*nav_buttons[i:i + 2])

    builder.row(
        InlineKeyboardButton(
            text="💬 Связь с автором (@yapsychokid)",
            url="https://t.me/yapsychokid"
        )
    )

    if show_back_to_menu:
        builder.row(
            InlineKeyboardButton(
                text="🏠 В главное меню",
                callback_data=NavigationCallback(to="main_menu").pack()
            )
        )

    return builder.as_markup()


# -------------------------------------------------------------------------
# Главное меню (ReplyKeyboard)
# -------------------------------------------------------------------------

def get_language_inline_keyboard() -> InlineKeyboardMarkup:
    """
    Клавиатура выбора языка (Русский / English) в стиле Bonch Bot.
    """
    builder = InlineKeyboardBuilder()
    builder.row(
        InlineKeyboardButton(
            text="🇷🇺 Русский",
            callback_data=LanguageCallback(lang="ru").pack()
        ),
        InlineKeyboardButton(
            text="🇬🇧 English",
            callback_data=LanguageCallback(lang="en").pack()
        ),
    )
    return builder.as_markup()


def get_notification_lead_time_keyboard(lang: str = "ru") -> InlineKeyboardMarkup:
    """
    Клавиатура настройки времени уведомлений перед занятиями (Bonch Bot style).
    - За 5 минут / За 10 минут
    - За 15 минут / За 20 минут
    - За 30 минут / За 45 минут
    - Не нужно
    """
    builder = InlineKeyboardBuilder()
    if lang == "en":
        builder.row(
            InlineKeyboardButton(text="⏱ 5 min before", callback_data=LeadTimeCallback(minutes=5).pack()),
            InlineKeyboardButton(text="⏱ 10 min before", callback_data=LeadTimeCallback(minutes=10).pack()),
        )
        builder.row(
            InlineKeyboardButton(text="⏱ 15 min before", callback_data=LeadTimeCallback(minutes=15).pack()),
            InlineKeyboardButton(text="⏱ 20 min before", callback_data=LeadTimeCallback(minutes=20).pack()),
        )
        builder.row(
            InlineKeyboardButton(text="⏱ 30 min before", callback_data=LeadTimeCallback(minutes=30).pack()),
            InlineKeyboardButton(text="⏱ 45 min before", callback_data=LeadTimeCallback(minutes=45).pack()),
        )
        builder.row(
            InlineKeyboardButton(text="❌ Not needed", callback_data=LeadTimeCallback(minutes=0).pack()),
        )
    else:
        builder.row(
            InlineKeyboardButton(text="⏱ За 5 минут", callback_data=LeadTimeCallback(minutes=5).pack()),
            InlineKeyboardButton(text="⏱ За 10 минут", callback_data=LeadTimeCallback(minutes=10).pack()),
        )
        builder.row(
            InlineKeyboardButton(text="⏱ За 15 минут", callback_data=LeadTimeCallback(minutes=15).pack()),
            InlineKeyboardButton(text="⏱ За 20 минут", callback_data=LeadTimeCallback(minutes=20).pack()),
        )
        builder.row(
            InlineKeyboardButton(text="⏱ За 30 минут", callback_data=LeadTimeCallback(minutes=30).pack()),
            InlineKeyboardButton(text="⏱ За 45 минут", callback_data=LeadTimeCallback(minutes=45).pack()),
        )
        builder.row(
            InlineKeyboardButton(text="❌ Не нужно", callback_data=LeadTimeCallback(minutes=0).pack()),
        )
    return builder.as_markup()


def get_main_reply_keyboard(
    notifications_enabled: bool = True, is_admin: bool = False, lang: str = "ru"
) -> ReplyKeyboardMarkup:
    """
    Основное меню бота с кнопками быстрого доступа к расписанию и настройкам:
    - Сегодня / Завтра
    - Где сейчас пара? / На неделю
    - Не иду на пару / Поиск преподавателя
    - Уведомления / Настройки и связь
    """
    builder = ReplyKeyboardBuilder()

    notif_text = "🔔 Уведомления: ВКЛ" if notifications_enabled else "🔕 Уведомления: ВЫКЛ"
    if lang == "en":
        notif_text = "🔔 Notifs: ON" if notifications_enabled else "🔕 Notifs: OFF"

    # Ряд 1: Расписание сегодня / завтра
    builder.row(
        KeyboardButton(text="📅 На сегодня" if lang == "ru" else "📅 Today"),
        KeyboardButton(text="📆 На завтра" if lang == "ru" else "🌅 Tomorrow"),
    )
    # Ряд 2: Где сейчас пара? / На неделю
    builder.row(
        KeyboardButton(text="📍 Где сейчас пара?" if lang == "ru" else "📍 Where is pair now?"),
        KeyboardButton(text="🗓 На неделю" if lang == "ru" else "📆 Full Week"),
    )
    # Ряд 3: Пропуск пар / Поиск преподавателя
    builder.row(
        KeyboardButton(text="💤 Не иду на пару" if lang == "ru" else "💤 Skip pair / Sleep"),
        KeyboardButton(text="🔍 Поиск преподавателя" if lang == "ru" else "🔍 Teacher search"),
    )
    # Ряд 4: Уведомления и подменю настроек/связи
    builder.row(
        KeyboardButton(text=notif_text),
        KeyboardButton(text="⚙️ Настройки и связь" if lang == "ru" else "⚙️ Settings & info"),
    )

    if is_admin:
        builder.row(
            KeyboardButton(text="👑 Админ-панель" if lang == "ru" else "👑 Admin Panel")
        )

    return builder.as_markup(resize_keyboard=True, is_persistent=True)


def get_settings_info_keyboard(lang: str = "ru") -> ReplyKeyboardMarkup:
    """
    Клавиатура подменю настроек, информации о боте и связи:
    - 👥 Моя подгруппа | ⚙️ Сменить группу
    - 🌐 Язык | ℹ️ Бот в группу
    - 👨‍💻 Связь с автором | 📖 Инструкция
    - ⬅️ Главное меню
    """
    builder = ReplyKeyboardBuilder()
    builder.row(
        KeyboardButton(text="👥 Моя подгруппа" if lang == "ru" else "👥 My subgroup"),
        KeyboardButton(text="⚙️ Сменить группу" if lang == "ru" else "⚙️ Change group"),
    )
    builder.row(
        KeyboardButton(text="🌐 Язык" if lang == "ru" else "🌐 Language"),
        KeyboardButton(text="ℹ️ Бот в группу" if lang == "ru" else "➕ Bot to group"),
    )
    builder.row(
        KeyboardButton(text="👨‍💻 Связь с автором" if lang == "ru" else "👨‍💻 Contact author"),
        KeyboardButton(text="📖 Инструкция" if lang == "ru" else "📖 Guide"),
    )
    builder.row(
        KeyboardButton(text="⬅️ Главное меню" if lang == "ru" else "⬅️ Main menu")
    )
    return builder.as_markup(resize_keyboard=True, is_persistent=True)


def get_group_menu_keyboard(lang: str = "ru") -> ReplyKeyboardMarkup:
    """
    Клавиатура для меню группы:
    - 📚 ДЗ 📚
    - 💬 Чат группы | 📝 Личные заметки
    - 🙋‍♂️ Староста 🙋‍♂️ | ⚙️ Сменить группу
    - ⬅️ Главное меню
    """
    builder = ReplyKeyboardBuilder()
    builder.row(
        KeyboardButton(text="📚 ДЗ 📚" if lang == "ru" else "📚 Homework 📚")
    )
    builder.row(
        KeyboardButton(text="💬 Чат группы" if lang == "ru" else "💬 Group chat"),
        KeyboardButton(text="📝 Личные заметки" if lang == "ru" else "📝 Personal notes"),
    )
    builder.row(
        KeyboardButton(text="🙋‍♂️ Староста 🙋‍♂️" if lang == "ru" else "🙋‍♂️ Starosta 🙋‍♂️"),
        KeyboardButton(text="⚙️ Сменить группу" if lang == "ru" else "⚙️ Change group"),
    )
    builder.row(
        KeyboardButton(text="⬅️ Главное меню" if lang == "ru" else "⬅️ Main menu")
    )
    return builder.as_markup(resize_keyboard=True, is_persistent=True)


def get_starosta_inline_keyboard(
    is_current_user: bool,
    has_starosta: bool,
    has_deputy: bool = False,
    is_deputy: bool = False,
    lang: str = "ru",
) -> InlineKeyboardMarkup:
    """
    Инлайн-клавиатура меню старосты:
    - Если старосты нет: кнопка подачи заявки на старосту («🙋‍♂️ Подать заявку на старосту 🙋‍♂️»)
    - Если пользователь староста: «✏️ Добавить ДЗ», «🗑 Удалить ДЗ», назначение/снятие зама, запрос на снятие
    - Если пользователь зам: «✏️ Добавить ДЗ», «🗑 Удалить ДЗ»
    - Если другой студент: возврат в главное меню
    """
    builder = InlineKeyboardBuilder()

    if not has_starosta:
        builder.row(
            InlineKeyboardButton(
                text="🙋‍♂️ Подать заявку на старосту 🙋‍♂️" if lang == "ru" else "🙋‍♂️ Apply for Starosta 🙋‍♂️",
                callback_data=StarostaCallback(action="claim").pack()
            )
        )
    elif is_current_user:
        builder.row(
            InlineKeyboardButton(
                text="✏️ Добавить ДЗ" if lang == "ru" else "✏️ Add Homework",
                callback_data=HomeworkCallback(action="add").pack()
            ),
            InlineKeyboardButton(
                text="🗑 Удалить ДЗ" if lang == "ru" else "🗑 Delete Homework",
                callback_data=HomeworkCallback(action="del_pick").pack()
            ),
        )
        if not has_deputy:
            builder.row(
                InlineKeyboardButton(
                    text="➕ Назначить заместителя" if lang == "ru" else "➕ Appoint Deputy",
                    callback_data=StarostaCallback(action="add_deputy").pack()
                )
            )
        else:
            builder.row(
                InlineKeyboardButton(
                    text="❌ Снять заместителя" if lang == "ru" else "❌ Remove Deputy",
                    callback_data=StarostaCallback(action="remove_deputy").pack()
                )
            )
        builder.row(
            InlineKeyboardButton(
                text="📨 Запрос на снятие старосты" if lang == "ru" else "📨 Request resignation",
                callback_data=StarostaCallback(action="resign").pack()
            )
        )
    elif is_deputy:
        builder.row(
            InlineKeyboardButton(
                text="✏️ Добавить ДЗ" if lang == "ru" else "✏️ Add Homework",
                callback_data=HomeworkCallback(action="add").pack()
            ),
            InlineKeyboardButton(
                text="🗑 Удалить ДЗ" if lang == "ru" else "🗑 Delete Homework",
                callback_data=HomeworkCallback(action="del_pick").pack()
            ),
        )

    builder.row(
        InlineKeyboardButton(
            text="🏠 В главное меню" if lang == "ru" else "🏠 Main menu",
            callback_data=NavigationCallback(to="main_menu").pack()
        )
    )
    return builder.as_markup()


def get_admin_starosta_decision_keyboard(
    req_id: int, group_id: str, candidate_id: int
) -> InlineKeyboardMarkup:
    """
    Клавиатура для администратора бота для одобрения или отклонения заявки на старосту.
    """
    builder = InlineKeyboardBuilder()
    builder.row(
        InlineKeyboardButton(
            text="✅ Одобрить",
            callback_data=AdminStarostaApproveCallback(
                action="approve", req_id=req_id, group_id=group_id, candidate_id=candidate_id
            ).pack(),
        ),
        InlineKeyboardButton(
            text="❌ Отклонить",
            callback_data=AdminStarostaApproveCallback(
                action="reject", req_id=req_id, group_id=group_id, candidate_id=candidate_id
            ).pack(),
        ),
    )
    return builder.as_markup()


def get_admin_starosta_remove_keyboard(
    group_id: str, starosta_id: int
) -> InlineKeyboardMarkup:
    """
    Клавиатура подтверждения снятия старосты администратором.
    """
    builder = InlineKeyboardBuilder()
    builder.row(
        InlineKeyboardButton(
            text="🗑 Снять старосту",
            callback_data=AdminStarostaApproveCallback(
                action="rem_confirm", req_id=0, group_id=group_id, candidate_id=starosta_id
            ).pack(),
        ),
        InlineKeyboardButton(
            text="❌ Отмена",
            callback_data=AdminStarostaApproveCallback(
                action="rem_cancel", req_id=0, group_id=group_id, candidate_id=starosta_id
            ).pack(),
        ),
    )
    return builder.as_markup()


def get_homework_list_keyboard(
    homework_list: list[dict[str, Any]], is_starosta: bool = False, lang: str = "ru"
) -> InlineKeyboardMarkup:
    """
    Инлайн-клавиатура просмотра домашних заданий.
    Если пользователь староста — добавляются кнопки создания/удаления ДЗ.
    """
    builder = InlineKeyboardBuilder()

    if is_starosta:
        builder.row(
            InlineKeyboardButton(
                text="✏️ Добавить ДЗ" if lang == "ru" else "✏️ Add Homework",
                callback_data=HomeworkCallback(action="add").pack()
            ),
        )
        if homework_list:
            builder.row(
                InlineKeyboardButton(
                    text="🗑 Удалить ДЗ" if lang == "ru" else "🗑 Delete Homework",
                    callback_data=HomeworkCallback(action="del_pick").pack()
                )
            )

    builder.row(
        InlineKeyboardButton(
            text="🏠 В главное меню" if lang == "ru" else "🏠 Main menu",
            callback_data=NavigationCallback(to="main_menu").pack()
        )
    )
    return builder.as_markup()


def get_homework_delete_keyboard(
    homework_list: list[dict[str, Any]], lang: str = "ru"
) -> InlineKeyboardMarkup:
    """
    Инлайн-клавиатура со списком заданий для удаления старостой.
    """
    builder = InlineKeyboardBuilder()

    for hw in homework_list:
        subj = hw.get("subject", "")
        due = hw.get("due_date", "")
        short_subj = subj if len(subj) <= 15 else subj[:13] + ".."
        btn_text = f"🗑 {short_subj} ({due})"
        builder.row(
            InlineKeyboardButton(
                text=btn_text,
                callback_data=HomeworkCallback(action="del", hw_id=hw["id"]).pack()
            )
        )

    builder.row(
        InlineKeyboardButton(
            text="❌ Отмена" if lang == "ru" else "❌ Cancel",
            callback_data=HomeworkCallback(action="cancel").pack()
        )
    )
    return builder.as_markup()


def get_homework_date_suggestions_keyboard(lang: str = "ru") -> InlineKeyboardMarkup:
    """
    Клавиатура с быстрыми вариантами срока сдачи ДЗ (Завтра, Послезавтра, След. понедельник).
    """
    builder = InlineKeyboardBuilder()
    today = date.today()
    tomorrow = today + timedelta(days=1)
    day_after = today + timedelta(days=2)

    # Следующий понедельник
    days_ahead = 7 - today.weekday() if today.weekday() != 0 else 7
    next_monday = today + timedelta(days=days_ahead)

    tomorrow_label = f"Завтра ({tomorrow.strftime('%d.%m')})" if lang == "ru" else f"Tomorrow ({tomorrow.strftime('%d.%m')})"
    day_after_label = f"Послезавтра ({day_after.strftime('%d.%m')})" if lang == "ru" else f"In 2 days ({day_after.strftime('%d.%m')})"
    mon_label = f"След. Пн ({next_monday.strftime('%d.%m')})" if lang == "ru" else f"Next Mon ({next_monday.strftime('%d.%m')})"

    builder.row(
        InlineKeyboardButton(
            text=tomorrow_label,
            callback_data=HwDateCallback(date_str=tomorrow.strftime("%d.%m.%Y")).pack()
        ),
        InlineKeyboardButton(
            text=day_after_label,
            callback_data=HwDateCallback(date_str=day_after.strftime("%d.%m.%Y")).pack()
        ),
    )
    builder.row(
        InlineKeyboardButton(
            text=mon_label,
            callback_data=HwDateCallback(date_str=next_monday.strftime("%d.%m.%Y")).pack()
        )
    )
    builder.row(
        InlineKeyboardButton(
            text="❌ Отмена" if lang == "ru" else "❌ Cancel",
            callback_data=HomeworkCallback(action="cancel").pack()
        )
    )
    return builder.as_markup()


def get_homework_subjects_inline_keyboard(subjects: list[str], lang: str = "ru") -> InlineKeyboardMarkup:
    """
    Клавиатура со списком предметов группы для быстрого выбора при добавлении ДЗ.
    """
    builder = InlineKeyboardBuilder()
    row: list[InlineKeyboardButton] = []
    for idx, subj in enumerate(subjects):
        display_name = subj if len(subj) <= 22 else subj[:20] + ".."
        row.append(
            InlineKeyboardButton(
                text=f"📖 {display_name}",
                callback_data=HwSubjectCallback(idx=idx).pack()
            )
        )
        if len(row) == 2:
            builder.row(*row)
            row = []
    if row:
        builder.row(*row)

    builder.row(
        InlineKeyboardButton(
            text="❌ Отмена" if lang == "ru" else "❌ Cancel",
            callback_data=HomeworkCallback(action="cancel").pack()
        )
    )
    return builder.as_markup()


def get_teacher_cancel_keyboard() -> InlineKeyboardMarkup:
    """
    Клавиатура отмены ввода при поиске преподавателя.
    """
    builder = InlineKeyboardBuilder()
    builder.row(
        InlineKeyboardButton(
            text="❌ Отмена поиска",
            callback_data=NavigationCallback(to="main_menu").pack()
        )
    )
    return builder.as_markup()


def get_teacher_search_choice_keyboard() -> InlineKeyboardMarkup:
    """
    Клавиатура выбора способа поиска: по ФИО преподавателя или по предмету своей группы.
    """
    builder = InlineKeyboardBuilder()
    builder.row(
        InlineKeyboardButton(
            text="🔍 Поиск по ФИО преподавателя",
            callback_data=TeacherChoiceCallback(action="fio").pack()
        )
    )
    builder.row(
        InlineKeyboardButton(
            text="📖 Преподаватели моей группы (по предметам)",
            callback_data=TeacherChoiceCallback(action="subjects").pack()
        )
    )
    builder.row(
        InlineKeyboardButton(
            text="❌ Отмена",
            callback_data=NavigationCallback(to="main_menu").pack()
        )
    )
    return builder.as_markup()


def get_group_subjects_inline_keyboard(subjects: list[str]) -> InlineKeyboardMarkup:
    """
    Клавиатура со списком предметов группы по 2 в ряд.
    """
    builder = InlineKeyboardBuilder()
    row: list[InlineKeyboardButton] = []
    for idx, subj in enumerate(subjects):
        display_name = subj if len(subj) <= 22 else subj[:20] + ".."
        row.append(
            InlineKeyboardButton(
                text=f"📖 {display_name}",
                callback_data=SubjectCallback(idx=idx).pack()
            )
        )
        if len(row) == 2:
            builder.row(*row)
            row = []
    if row:
        builder.row(*row)

    builder.row(
        InlineKeyboardButton(
            text="⬅️ Назад к выбору поиска",
            callback_data=TeacherChoiceCallback(action="menu").pack()
        )
    )
    builder.row(
        InlineKeyboardButton(
            text="🏠 В главное меню",
            callback_data=NavigationCallback(to="main_menu").pack()
        )
    )
    return builder.as_markup()


def get_add_to_group_keyboard(bot_username: str) -> InlineKeyboardMarkup:
    """
    Клавиатура со ссылкой для быстрого добавления бота в группу/беседу в 1 клик.
    """
    builder = InlineKeyboardBuilder()
    builder.row(
        InlineKeyboardButton(
            text="➕ Добавить бота в беседу / группу",
            url=f"https://t.me/{bot_username}?startgroup=true"
        )
    )
    builder.row(
        InlineKeyboardButton(
            text="💬 Связь с автором (@yapsychokid)",
            url="https://t.me/yapsychokid"
        )
    )
    builder.row(
        InlineKeyboardButton(
            text="🏠 В главное меню",
            callback_data=NavigationCallback(to="main_menu").pack()
        )
    )
    return builder.as_markup()


# -------------------------------------------------------------------------
# Клавиатуры админ-панели
# -------------------------------------------------------------------------

def get_admin_main_inline_keyboard() -> InlineKeyboardMarkup:
    """
    Главная клавиатура админ-панели управления ботом.
    """
    builder = InlineKeyboardBuilder()

    builder.row(
        InlineKeyboardButton(
            text="📊 Статистика",
            callback_data=AdminCallback(action="stats").pack()
        ),
        InlineKeyboardButton(
            text="👥 Топ групп",
            callback_data=AdminCallback(action="groups").pack()
        ),
    )
    builder.row(
        InlineKeyboardButton(
            text="📋 Последние пользователи",
            callback_data=AdminCallback(action="users").pack()
        ),
        InlineKeyboardButton(
            text="📢 Рассылка всем",
            callback_data=AdminCallback(action="broadcast").pack()
        ),
    )
    builder.row(
        InlineKeyboardButton(
            text="⚡ Прогреть кэш",
            callback_data=AdminCallback(action="warmup").pack()
        ),
        InlineKeyboardButton(
            text="🗑 Очистить кэш",
            callback_data=AdminCallback(action="clearcache").pack()
        ),
    )
    builder.row(
        InlineKeyboardButton(
            text="💾 Скачать БД",
            callback_data=AdminCallback(action="export_db").pack()
        ),
        InlineKeyboardButton(
            text="📥 Загрузить БД",
            callback_data=AdminCallback(action="import_db").pack()
        ),
    )
    builder.row(
        InlineKeyboardButton(
            text="❌ Закрыть панель",
            callback_data=AdminCallback(action="close").pack()
        )
    )

    return builder.as_markup()


def get_admin_back_inline_keyboard() -> InlineKeyboardMarkup:
    """
    Кнопка возврата в меню админ-панели.
    """
    builder = InlineKeyboardBuilder()
    builder.row(
        InlineKeyboardButton(
            text="⬅️ Назад в админку",
            callback_data=AdminCallback(action="menu").pack()
        )
    )
    return builder.as_markup()


def get_broadcast_cancel_inline_keyboard() -> InlineKeyboardMarkup:
    """
    Кнопка отмены режима рассылки.
    """
    builder = InlineKeyboardBuilder()
    builder.row(
        InlineKeyboardButton(
            text="❌ Отмена рассылки",
            callback_data=AdminCallback(action="menu").pack()
        )
    )
    return builder.as_markup()


# -------------------------------------------------------------------------
# ЗВОНКИ, ЧАТ ГРУППЫ, НАВИГАТОР, ПРЕПОДАВАТЕЛИ, СЕССИЯ, ЗАМЕТКИ, РЕГУЛИРОВКА
# -------------------------------------------------------------------------

BELLS_TIMETABLE: list[dict[str, Any]] = [
    {"pair": 1, "start": "08:30", "end": "10:00", "break_min": 10},
    {"pair": 2, "start": "10:10", "end": "11:40", "break_min": 10},
    {"pair": 3, "start": "11:50", "end": "13:20", "break_min": 40, "is_big_break": True},
    {"pair": 4, "start": "14:00", "end": "15:30", "break_min": 10},
    {"pair": 5, "start": "15:40", "end": "17:10", "break_min": 10},
    {"pair": 6, "start": "17:20", "end": "18:50", "break_min": 10},
    {"pair": 7, "start": "19:00", "end": "20:30", "break_min": 0},
]


def get_bells_inline_keyboard(lang: str = "ru") -> InlineKeyboardMarkup:
    """Клавиатура карточки расписания звонков."""
    builder = InlineKeyboardBuilder()
    builder.row(
        InlineKeyboardButton(
            text="🔄 Обновить статус" if lang == "ru" else "🔄 Refresh status",
            callback_data=BellsCallback(action="refresh").pack()
        )
    )
    builder.row(
        InlineKeyboardButton(
            text="🏠 В главное меню" if lang == "ru" else "🏠 Main menu",
            callback_data=NavigationCallback(to="main_menu").pack()
        )
    )
    return builder.as_markup()


def get_group_chat_link_keyboard(
    chat_link: Optional[str] = None, is_starosta: bool = False, lang: str = "ru"
) -> InlineKeyboardMarkup:
    """Клавиатура ссылки на официальную беседу группы."""
    builder = InlineKeyboardBuilder()
    if chat_link:
        builder.row(
            InlineKeyboardButton(
                text="🚀 Перейти в беседу группы" if lang == "ru" else "🚀 Open group chat",
                url=chat_link
            )
        )
    if is_starosta:
        btn_label = "✏️ Изменить ссылку" if chat_link else "✏️ Указать ссылку на чат"
        if lang == "en":
            btn_label = "✏️ Edit chat link" if chat_link else "✏️ Set chat link"
        builder.row(
            InlineKeyboardButton(
                text=btn_label,
                callback_data=GroupChatLinkCallback(action="set").pack()
            )
        )
        if chat_link:
            builder.row(
                InlineKeyboardButton(
                    text="🗑 Удалить ссылку" if lang == "ru" else "🗑 Remove link",
                    callback_data=GroupChatLinkCallback(action="del").pack()
                )
            )
    builder.row(
        InlineKeyboardButton(
            text="🏠 В главное меню" if lang == "ru" else "🏠 Main menu",
            callback_data=NavigationCallback(to="main_menu").pack()
        )
    )
    return builder.as_markup()


def get_campus_inline_keyboard(
    current_floor: int = 1, lang: str = "ru", webapp_url: Optional[str] = None
) -> InlineKeyboardMarkup:
    """Клавиатура справочника-навигатора по корпусу колледжа."""
    builder = InlineKeyboardBuilder()
    floor_btns: list[InlineKeyboardButton] = []
    for f in range(1, 5):
        label = f"• {f} этаж •" if f == current_floor else f"{f} этаж"
        if lang == "en":
            label = f"• Floor {f} •" if f == current_floor else f"Floor {f}"
        floor_btns.append(
            InlineKeyboardButton(
                text=label,
                callback_data=CampusCallback(action="floor", floor=f).pack()
            )
        )
    builder.row(*floor_btns[:2])
    builder.row(*floor_btns[2:])
    builder.row(
        InlineKeyboardButton(
            text="🔍 Найти аудиторию / кабинет" if lang == "ru" else "🔍 Find classroom / room",
            callback_data=CampusCallback(action="search", floor=current_floor).pack()
        )
    )
    if webapp_url:
        builder.row(
            InlineKeyboardButton(
                text="🗺 Открыть карту колледжа (Mini App)" if lang == "ru" else "🗺 Open Map (Mini App)",
                web_app=WebAppInfo(url=webapp_url)
            )
        )
    builder.row(
        InlineKeyboardButton(
            text="🏠 В главное меню" if lang == "ru" else "🏠 Main menu",
            callback_data=NavigationCallback(to="main_menu").pack()
        )
    )
    return builder.as_markup()


def get_my_teachers_keyboard(teachers: list[dict[str, Any]], lang: str = "ru") -> InlineKeyboardMarkup:
    """Клавиатура со списком преподавателей группы для быстрого просмотра их расписания."""
    builder = InlineKeyboardBuilder()
    for idx, t in enumerate(teachers):
        name = t.get("name", "Преподаватель")
        display_name = name if len(name) <= 24 else name[:22] + ".."
        builder.row(
            InlineKeyboardButton(
                text=f"👨‍🏫 {display_name}",
                callback_data=MyTeacherCallback(idx=idx).pack()
            )
        )
    builder.row(
        InlineKeyboardButton(
            text="🏠 В главное меню" if lang == "ru" else "🏠 Main menu",
            callback_data=NavigationCallback(to="main_menu").pack()
        )
    )
    return builder.as_markup()


def get_exams_list_keyboard(
    exams: list[dict[str, Any]], is_starosta: bool = False, lang: str = "ru"
) -> InlineKeyboardMarkup:
    """Клавиатура просмотра сессии и экзаменов."""
    builder = InlineKeyboardBuilder()
    if is_starosta:
        builder.row(
            InlineKeyboardButton(
                text="➕ Добавить экзамен / зачет" if lang == "ru" else "➕ Add exam / test",
                callback_data=ExamsCallback(action="add").pack()
            )
        )
        if exams:
            builder.row(
                InlineKeyboardButton(
                    text="🗑 Удалить экзамен" if lang == "ru" else "🗑 Delete exam",
                    callback_data=ExamsCallback(action="del_pick").pack()
                )
            )
    builder.row(
        InlineKeyboardButton(
            text="🏠 В главное меню" if lang == "ru" else "🏠 Main menu",
            callback_data=NavigationCallback(to="main_menu").pack()
        )
    )
    return builder.as_markup()


def get_exams_delete_keyboard(exams: list[dict[str, Any]], lang: str = "ru") -> InlineKeyboardMarkup:
    """Клавиатура со списком экзаменов для удаления."""
    builder = InlineKeyboardBuilder()
    for ex in exams:
        subj = ex.get("subject", "")
        dt = ex.get("exam_date", "")
        short_subj = subj if len(subj) <= 16 else subj[:14] + ".."
        btn_text = f"🗑 {short_subj} ({dt})"
        builder.row(
            InlineKeyboardButton(
                text=btn_text,
                callback_data=ExamsCallback(action="del", exam_id=ex["id"]).pack()
            )
        )
    builder.row(
        InlineKeyboardButton(
            text="❌ Отмена" if lang == "ru" else "❌ Cancel",
            callback_data=ExamsCallback(action="cancel").pack()
        )
    )
    return builder.as_markup()


def get_user_notes_keyboard(notes: list[dict[str, Any]], lang: str = "ru") -> InlineKeyboardMarkup:
    """Клавиатура персонального блокнота заметок и дедлайнов."""
    builder = InlineKeyboardBuilder()
    builder.row(
        InlineKeyboardButton(
            text="➕ Добавить заметку" if lang == "ru" else "➕ Add note",
            callback_data=NotesCallback(action="add").pack()
        )
    )
    if notes:
        builder.row(
            InlineKeyboardButton(
                text="🗑 Удалить заметку" if lang == "ru" else "🗑 Delete note",
                callback_data=NotesCallback(action="del_pick").pack()
            )
        )
    builder.row(
        InlineKeyboardButton(
            text="🏠 В главное меню" if lang == "ru" else "🏠 Main menu",
            callback_data=NavigationCallback(to="main_menu").pack()
        )
    )
    return builder.as_markup()


def get_user_notes_delete_keyboard(notes: list[dict[str, Any]], lang: str = "ru") -> InlineKeyboardMarkup:
    """Клавиатура со списком заметок для удаления."""
    builder = InlineKeyboardBuilder()
    for nt in notes:
        text = nt.get("note_text", "").strip().replace("\n", " ")
        short_text = text if len(text) <= 22 else text[:20] + ".."
        btn_text = f"🗑 {short_text}"
        builder.row(
            InlineKeyboardButton(
                text=btn_text,
                callback_data=NotesCallback(action="del", note_id=nt["id"]).pack()
            )
        )
    builder.row(
        InlineKeyboardButton(
            text="❌ Отмена" if lang == "ru" else "❌ Cancel",
            callback_data=NotesCallback(action="cancel").pack()
        )
    )
    return builder.as_markup()


def get_note_date_quick_keyboard(lang: str = "ru") -> InlineKeyboardMarkup:
    """Быстрый выбор даты дедлайна для заметки (Сегодня, Завтра, Послезавтра или без даты)."""
    from datetime import date, timedelta
    today = date.today()
    tomorrow = today + timedelta(days=1)
    day_after = today + timedelta(days=2)

    builder = InlineKeyboardBuilder()
    builder.row(
        InlineKeyboardButton(
            text=f"📅 Сегодня ({today.strftime('%d.%m')})" if lang == "ru" else f"📅 Today ({today.strftime('%d.%m')})",
            callback_data=NoteDateCallback(date_str=today.isoformat()).pack(),
        ),
        InlineKeyboardButton(
            text=f"📆 Завтра ({tomorrow.strftime('%d.%m')})" if lang == "ru" else f"📆 Tomorrow ({tomorrow.strftime('%d.%m')})",
            callback_data=NoteDateCallback(date_str=tomorrow.isoformat()).pack(),
        ),
    )
    builder.row(
        InlineKeyboardButton(
            text=f"🗓 Послезавтра ({day_after.strftime('%d.%m')})" if lang == "ru" else f"🗓 In 2 days ({day_after.strftime('%d.%m')})",
            callback_data=NoteDateCallback(date_str=day_after.isoformat()).pack(),
        ),
        InlineKeyboardButton(
            text="⏭ Без даты" if lang == "ru" else "⏭ No date",
            callback_data=NoteDateCallback(date_str="none").pack(),
        ),
    )
    builder.row(
        InlineKeyboardButton(
            text="❌ Отмена" if lang == "ru" else "❌ Cancel",
            callback_data=NotesCallback(action="cancel").pack(),
        )
    )
    return builder.as_markup()


def get_note_pair_quick_keyboard(lang: str = "ru") -> InlineKeyboardMarkup:
    """Выбор пары для дедлайна заметки (1-7 пара, к началу дня или без пары)."""
    builder = InlineKeyboardBuilder()
    builder.row(
        InlineKeyboardButton(text="1 пара (08:30)", callback_data=NotePairCallback(pair=1).pack()),
        InlineKeyboardButton(text="2 пара (10:10)", callback_data=NotePairCallback(pair=2).pack()),
        InlineKeyboardButton(text="3 пара (11:50)", callback_data=NotePairCallback(pair=3).pack()),
    )
    builder.row(
        InlineKeyboardButton(text="4 пара (14:00)", callback_data=NotePairCallback(pair=4).pack()),
        InlineKeyboardButton(text="5 пара (15:40)", callback_data=NotePairCallback(pair=5).pack()),
        InlineKeyboardButton(text="6 пара (17:20)", callback_data=NotePairCallback(pair=6).pack()),
    )
    builder.row(
        InlineKeyboardButton(
            text="⏰ К началу дня" if lang == "ru" else "⏰ Before classes",
            callback_data=NotePairCallback(pair=0).pack(),
        ),
        InlineKeyboardButton(
            text="⏭ Без пары" if lang == "ru" else "⏭ Any time",
            callback_data=NotePairCallback(pair=-1).pack(),
        ),
    )
    builder.row(
        InlineKeyboardButton(
            text="❌ Отмена" if lang == "ru" else "❌ Cancel",
            callback_data=NotesCallback(action="cancel").pack(),
        )
    )
    return builder.as_markup()


def get_notification_settings_keyboard(
    evening_time: str, morning_time: str, lead_min: int, enabled: bool, lang: str = "ru"
) -> InlineKeyboardMarkup:
    """Интерактивное меню тонкой регулировки времени рассылок и уведомлений."""
    builder = InlineKeyboardBuilder()

    status_text = "🔔 Статус рассылок: ВКЛ" if enabled else "🔕 Статус рассылок: ВЫКЛ"
    if lang == "en":
        status_text = "🔔 Notifications: ON" if enabled else "🔕 Notifications: OFF"

    builder.row(
        InlineKeyboardButton(
            text=status_text,
            callback_data=NotificationSettingCallback(target="toggle").pack()
        )
    )

    ev_val = evening_time if evening_time and evening_time != "off" else ("Выкл" if lang == "ru" else "Off")
    ev_label = f"🌙 На завтра: {ev_val}" if lang == "ru" else f"🌙 Tomorrow: {ev_val}"
    builder.row(
        InlineKeyboardButton(
            text=ev_label,
            callback_data=NotificationSettingCallback(target="evening_menu").pack()
        )
    )

    mr_val = morning_time if morning_time and morning_time != "off" else ("Выкл" if lang == "ru" else "Off")
    mr_label = f"☀️ На сегодня: {mr_val}" if lang == "ru" else f"☀️ Today: {mr_val}"
    builder.row(
        InlineKeyboardButton(
            text=mr_label,
            callback_data=NotificationSettingCallback(target="morning_menu").pack()
        )
    )

    lead_str = f"за {lead_min} мин" if lead_min > 0 else ("в момент звонка" if lead_min == 0 else "выкл")
    if lang == "en":
        lead_str = f"{lead_min}m before" if lead_min > 0 else "at bell"
    ld_label = f"⏱ До пары: {lead_str}" if lang == "ru" else f"⏱ Class reminder: {lead_str}"
    builder.row(
        InlineKeyboardButton(
            text=ld_label,
            callback_data=NotificationSettingCallback(target="lead_menu").pack()
        )
    )

    builder.row(
        InlineKeyboardButton(
            text="🏠 В главное меню" if lang == "ru" else "🏠 Main menu",
            callback_data=NavigationCallback(to="main_menu").pack()
        )
    )
    return builder.as_markup()


def get_evening_time_selection_keyboard(current_val: str, lang: str = "ru") -> InlineKeyboardMarkup:
    """Клавиатура выбора времени вечерней рассылки (на завтра)."""
    builder = InlineKeyboardBuilder()
    times = ["18:00", "19:00", "20:00", "21:00", "22:00", "off"]
    row: list[InlineKeyboardButton] = []
    for t in times:
        label = t if t != "off" else ("Выкл" if lang == "ru" else "Off")
        if t == current_val or (t == "20:00" and not current_val):
            label = f"✅ {label}"
        row.append(
            InlineKeyboardButton(
                text=label,
                callback_data=NotificationSettingCallback(target="evening_set", value=t).pack()
            )
        )
        if len(row) == 3:
            builder.row(*row)
            row = []
    if row:
        builder.row(*row)
    builder.row(
        InlineKeyboardButton(
            text="⬅️ Назад к настройкам" if lang == "ru" else "⬅️ Back to settings",
            callback_data=NotificationSettingCallback(target="menu").pack()
        )
    )
    return builder.as_markup()


def get_morning_time_selection_keyboard(current_val: str, lang: str = "ru") -> InlineKeyboardMarkup:
    """Клавиатура выбора времени утренней рассылки (на сегодня)."""
    builder = InlineKeyboardBuilder()
    times = ["07:00", "07:30", "08:00", "08:30", "09:00", "off"]
    row: list[InlineKeyboardButton] = []
    for t in times:
        label = t if t != "off" else ("Выкл" if lang == "ru" else "Off")
        if t == current_val or (t == "08:00" and not current_val):
            label = f"✅ {label}"
        row.append(
            InlineKeyboardButton(
                text=label,
                callback_data=NotificationSettingCallback(target="morning_set", value=t).pack()
            )
        )
        if len(row) == 3:
            builder.row(*row)
            row = []
    if row:
        builder.row(*row)
    builder.row(
        InlineKeyboardButton(
            text="⬅️ Назад к настройкам" if lang == "ru" else "⬅️ Back to settings",
            callback_data=NotificationSettingCallback(target="menu").pack()
        )
    )
    return builder.as_markup()


def get_subgroup_selection_keyboard(current_subgroup: int = 0, lang: str = "ru") -> InlineKeyboardMarkup:
    """Клавиатура выбора подгруппы студента."""
    builder = InlineKeyboardBuilder()

    opt1 = "✅ 1-я подгруппа" if current_subgroup == 1 else ("1-я подгруппа" if lang == "ru" else "1st subgroup")
    opt2 = "✅ 2-я подгруппа" if current_subgroup == 2 else ("2-я подгруппа" if lang == "ru" else "2nd subgroup")
    opt0 = "✅ Вся группа (обе)" if current_subgroup == 0 else ("Вся группа (обе)" if lang == "ru" else "Entire group (both)")

    builder.row(
        InlineKeyboardButton(
            text=opt1,
            callback_data=SubgroupCallback(action="set", subgroup=1).pack()
        ),
        InlineKeyboardButton(
            text=opt2,
            callback_data=SubgroupCallback(action="set", subgroup=2).pack()
        )
    )
    builder.row(
        InlineKeyboardButton(
            text=opt0,
            callback_data=SubgroupCallback(action="set", subgroup=0).pack()
        )
    )
    builder.row(
        InlineKeyboardButton(
            text="⬅️ Закрыть" if lang == "ru" else "⬅️ Close",
            callback_data=SubgroupCallback(action="close", subgroup=current_subgroup).pack()
        )
    )
    return builder.as_markup()


def get_skip_pair_keyboard(
    lessons: list[dict[str, Any]],
    skipped_pairs: set[int],
    date_str: str,
    lang: str = "ru",
) -> InlineKeyboardMarkup:
    """
    Клавиатура интерактивного выбора пропущенных пар на конкретную дату.
    Показывает пары с чекбоксом (✅ Иду / 💤 Пропускаю), а также кнопку «😴 Сплю до 2-й пары».
    """
    builder = InlineKeyboardBuilder()

    # Сгруппируем уникальные номера пар
    unique_pairs: dict[int, str] = {}
    for l in lessons:
        p_num = l.get("pair_number")
        if p_num is not None and p_num not in unique_pairs:
            subj = l.get("subject", f"Пара {p_num}")
            if len(subj) > 18:
                subj = subj[:15] + "..."
            unique_pairs[p_num] = subj

    for p_num in sorted(unique_pairs.keys()):
        subj = unique_pairs[p_num]
        is_skipped = p_num in skipped_pairs
        status_icon = "💤 Пропуск" if is_skipped else "✅ Иду"
        btn_text = f"{status_icon} | {p_num} пара: {subj}"
        builder.row(
            InlineKeyboardButton(
                text=btn_text,
                callback_data=SkipPairCallback(
                    action="toggle", pair_number=p_num, date_str=date_str
                ).pack()
            )
        )

    # Быстрые действия
    has_first_pair = 1 in unique_pairs
    if has_first_pair:
        first_is_skipped = 1 in skipped_pairs
        sleep_text = "⏰ Проснулся (иду на 1-ю)" if first_is_skipped else "😴 Сплю до 2-й пары"
        builder.row(
            InlineKeyboardButton(
                text=sleep_text,
                callback_data=SkipPairCallback(
                    action="sleep_first", pair_number=1, date_str=date_str
                ).pack()
            )
        )

    if skipped_pairs:
        builder.row(
            InlineKeyboardButton(
                text="🔄 Иду на все пары" if lang == "ru" else "🔄 Attending all classes",
                callback_data=SkipPairCallback(
                    action="clear", pair_number=0, date_str=date_str
                ).pack()
            )
        )

    builder.row(
        InlineKeyboardButton(
            text="⬅️ Закрыть" if lang == "ru" else "⬅️ Close",
            callback_data=SkipPairCallback(
                action="close", pair_number=0, date_str=date_str
            ).pack()
        )
    )

    return builder.as_markup()


def pack_schedule_for_webapp(
    week_days: list[dict[str, Any]],
    group_name: str,
    week_number: int = 1,
    is_even: bool = False,
    subgroup: int = 0,
    user_id: Optional[int] = None,
    skipped_pairs: Optional[list[str]] = None,
) -> str:
    """Упаковывает расписание группы в компактную base64url JSON строку для мгновенной загрузки в WebApp."""
    import base64
    import json

    minimal_days = []
    for day in week_days:
        lessons = []
        for l in day.get("lessons", []):
            lessons.append({
                "pair_number": l.get("pair_number"),
                "time": l.get("time", ""),
                "subject": l.get("subject", ""),
                "lesson_type": l.get("lesson_type", ""),
                "room": l.get("room", ""),
                "teacher": l.get("teacher", ""),
                "subgroup": l.get("subgroup", 0),
                "is_external": l.get("is_external", False),
            })
        minimal_days.append({
            "date": day.get("date", ""),
            "day_name": day.get("day_name", ""),
            "lessons": lessons,
        })

    payload: dict[str, Any] = {
        "group_name": group_name,
        "week_number": week_number,
        "is_even": is_even,
        "subgroup": subgroup,
        "days": minimal_days,
    }
    if user_id:
        payload["user_id"] = user_id
    if skipped_pairs:
        payload["skipped_pairs"] = skipped_pairs

    raw_json = json.dumps(payload, ensure_ascii=False)
    b64 = base64.urlsafe_b64encode(raw_json.encode("utf-8")).decode("ascii")
    return b64.rstrip("=")


def build_webapp_url(
    group_name: str,
    subgroup: int = 0,
    week_days: Optional[list[dict[str, Any]]] = None,
    week_number: int = 1,
    is_even: bool = False,
    base_url: Optional[str] = None,
    user_id: Optional[int] = None,
    skipped_pairs: Optional[list[str]] = None,
) -> str:
    """Строит полный HTTPS URL для Telegram WebApp с опциональным hash payload данных."""
    from config import Settings
    if not base_url:
        settings = Settings()
        base_url = settings.WEBAPP_SCHEDULE_URL or "https://ktmu-schedule-bot-plqd-one.vercel.app/"

    if week_days:
        b64_data = pack_schedule_for_webapp(
            week_days, group_name, week_number, is_even, subgroup,
            user_id=user_id, skipped_pairs=skipped_pairs
        )
        return f"{base_url}#data={b64_data}"

    query = f"?group={group_name}&subgroup={subgroup}"
    if user_id:
        query += f"&user_id={user_id}"
    return f"{base_url}{query}"


def get_webapp_inline_keyboard(url: str, lang: str = "ru") -> InlineKeyboardMarkup:
    """Клавиатура с кнопкой вызова Telegram Mini App."""
    builder = InlineKeyboardBuilder()
    builder.row(
        InlineKeyboardButton(
            text="📱 Открыть расписание" if lang == "ru" else "📱 Open Schedule WebApp",
            web_app=WebAppInfo(url=url)
        )
    )
    return builder.as_markup()


