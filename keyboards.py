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


# -------------------------------------------------------------------------
# Главное меню (ReplyKeyboard)
# -------------------------------------------------------------------------

def get_main_reply_keyboard(notifications_enabled: bool = True) -> ReplyKeyboardMarkup:
    """
    Основное меню бота с кнопками быстрого доступа к расписанию и настройкам.
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
    )

    return builder.as_markup(resize_keyboard=True)
