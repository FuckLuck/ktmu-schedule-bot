from typing import Any
from aiogram.filters.callback_data import CallbackData
from aiogram.types import (
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    KeyboardButton,
    ReplyKeyboardMarkup,
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
    Располагает группы по 3 в ряд. Содержит кнопки «Назад к специальностям» и «В главное меню».
    """
    builder = InlineKeyboardBuilder()

    # Сортируем группы по названию
    sorted_groups = sorted(groups, key=lambda g: g.get("group_name", ""))

    # Добавляем группы по 3 в ряд
    row: list[InlineKeyboardButton] = []
    for g in sorted_groups:
        row.append(
            InlineKeyboardButton(
                text=f"🎓 {g['group_name']}",
                callback_data=GroupCallback(action="pick", group_id=g["id"]).pack()
            )
        )
        if len(row) == 3:
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


def get_schedule_nav_keyboard(
    current_date_str: str,
    show_today_past: bool = False,
) -> InlineKeyboardMarkup:
    """
    Инлайн-клавиатура для сообщений расписания:
    - Переключение между сегодня и завтра
    - Кнопка просмотра прошедшего расписания за сегодня (если пары уже кончились)
    - Кнопка «Назад в главное меню»
    """
    builder = InlineKeyboardBuilder()

    if show_today_past:
        builder.row(
            InlineKeyboardButton(
                text="⏪ Показать прошедшее за сегодня",
                callback_data=ScheduleNavCallback(action="today_past", date_str=current_date_str).pack()
            )
        )

    builder.row(
        InlineKeyboardButton(
            text="📆 На завтра",
            callback_data=ScheduleNavCallback(action="tomorrow", date_str=current_date_str).pack()
        ),
        InlineKeyboardButton(
            text="🗓 На неделю",
            callback_data=ScheduleNavCallback(action="week", date_str=current_date_str).pack()
        ),
    )

    builder.row(
        InlineKeyboardButton(
            text="⬅️ Назад в меню",
            callback_data=NavigationCallback(to="main_menu").pack()
        )
    )

    return builder.as_markup()


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

def get_main_reply_keyboard(
    notifications_enabled: bool = True, is_admin: bool = False
) -> ReplyKeyboardMarkup:
    """
    Основное меню бота с кнопками быстрого доступа к расписанию и настройкам.
    Для администраторов автоматически добавляется кнопка входа в админ-панель.
    """
    builder = ReplyKeyboardBuilder()

    notif_text = "🔔 Уведомления: ВКЛ" if notifications_enabled else "🔕 Уведомления: ВЫКЛ"

    builder.row(
        KeyboardButton(text="📅 На сегодня"),
        KeyboardButton(text="📆 На завтра"),
    )
    builder.row(
        KeyboardButton(text="🗓 На неделю"),
        KeyboardButton(text="⚙️ Сменить группу"),
    )
    builder.row(
        KeyboardButton(text=notif_text),
        KeyboardButton(text="📖 Инструкция"),
    )
    builder.row(
        KeyboardButton(text="👨‍💻 Связь с автором"),
    )

    if is_admin:
        builder.row(
            KeyboardButton(text="👑 Админ-панель")
        )

    return builder.as_markup(resize_keyboard=True)


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
