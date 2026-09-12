"""
Модуль генерации красивых графических карточек расписания (PNG)
в современном стиле тёмной темы (Dark Theme) с использованием Pillow.
"""

import io
import os
from datetime import datetime
from pathlib import Path
from typing import Any

from PIL import Image, ImageDraw, ImageFont

# Цветовая палитра карточки (современная темная тема)
COLOR_BG = (18, 24, 38)            # Фон #121826
COLOR_CARD_BG = (30, 41, 59)       # Карточка пары #1E293B
COLOR_CARD_BORDER = (51, 65, 85)   # Граница #334155
COLOR_TEXT_WHITE = (248, 250, 252) # Белый текст #F8FAFC
COLOR_TEXT_MUTED = (148, 163, 184) # Серый подтекст #94A3B8
COLOR_ACCENT = (56, 189, 248)      # Голубой акцент #38BDF8
COLOR_GREEN = (52, 211, 153)       # Зеленый акцент #34D399
COLOR_ORANGE = (251, 146, 60)      # Оранжевый акцент #FB923C

# Цвета индикаторов пар
PAIR_COLORS = [
    (59, 130, 246),  # Синий
    (99, 102, 241),  # Индиго
    (14, 165, 233),  # Голубой
    (168, 85, 247),  # Фиолетовый
    (236, 72, 153),  # Розовый
    (20, 184, 166),  # Бирюзовый
    (245, 158, 11),  # Янтарный
]

MONTHS_RU = {
    1: "января", 2: "февраля", 3: "марта", 4: "апреля",
    5: "мая", 6: "июня", 7: "июля", 8: "августа",
    9: "сентября", 10: "октября", 11: "ноября", 12: "декабря"
}


BASE_DIR = Path(__file__).resolve().parent
FONTS_DIR = BASE_DIR / "assets" / "fonts"


def _get_font(size: int, bold: bool = False) -> ImageFont.FreeTypeFont | ImageFont.ImageFont:
    """Загружает шрифт с поддержкой кириллицы из assets/fonts, системы или fallback."""
    # 1. Приоритет: встроенные проверенные шрифты из assets/fonts/ (гарантируют кириллицу на любом хосте)
    bundled_names = (
        ["font_bold.ttf", "font.ttf"]
        if bold
        else ["font.ttf", "font_bold.ttf"]
    )
    for name in bundled_names:
        font_path = FONTS_DIR / name
        if font_path.exists():
            try:
                return ImageFont.truetype(str(font_path), size)
            except Exception:
                pass

    # 2. Системные шрифты Windows
    font_names = (
        ["segoeuib.ttf", "arialbd.ttf", "arial.ttf"]
        if bold
        else ["segoeui.ttf", "arial.ttf"]
    )
    for name in font_names:
        win_path = os.path.join("C:\\Windows\\Fonts", name)
        if os.path.exists(win_path):
            try:
                return ImageFont.truetype(win_path, size)
            except Exception:
                pass

    # 3. Linux / container fallback
    linux_paths = [
        f"/usr/share/fonts/truetype/dejavu/DejaVuSans{'-Bold' if bold else ''}.ttf",
        "/usr/share/fonts/truetype/liberation/LiberationSans-Regular.ttf",
        "/usr/share/fonts/truetype/freefont/FreeSans.ttf",
    ]
    for linux_path in linux_paths:
        if os.path.exists(linux_path):
            try:
                return ImageFont.truetype(linux_path, size)
            except Exception:
                pass

    return ImageFont.load_default()


def render_schedule_image(
    schedule_data: dict[str, Any], group_name: str, bot_username: str = "schedulektmubot"
) -> io.BytesIO:
    """
    Генерирует PNG-изображение карточки расписания на день.
    Возвращает io.BytesIO поток с готовым изображением.
    """
    date_str = schedule_data.get("date", "")
    day_name = schedule_data.get("day_name", "")
    week_num = schedule_data.get("week_number", 1)
    is_even = schedule_data.get("is_even_week", False)
    parity_str = "Чётная" if is_even else "Нечётная"
    lessons = schedule_data.get("lessons", [])

    # Форматирование даты
    formatted_date = date_str
    try:
        dt = datetime.strptime(date_str, "%Y-%m-%d")
        formatted_date = f"{dt.day} {MONTHS_RU.get(dt.month, '')} {dt.year}"
    except Exception:
        pass

    # Группируем пары по номеру и времени (для подгрупп)
    pairs_map: dict[tuple[int, str], list[dict[str, Any]]] = {}
    for idx, l in enumerate(lessons, start=1):
        p_num = l.get("pair_number", idx)
        p_time = l.get("time", "")
        pairs_map.setdefault((p_num, p_time), []).append(l)

    # Расчет высоты холста
    card_width = 850
    header_height = 140
    footer_height = 50

    card_heights = []
    for (p_num, _), pair_lessons in pairs_map.items():
        if len(pair_lessons) > 1:
            # Древовидные подгруппы: заголовок + строки подгрупп
            h = 60 + (len(pair_lessons) * 32) + 20
        else:
            h = 105
        card_heights.append(h)

    total_content_height = sum(card_heights) + (len(card_heights) * 15)
    if not pairs_map:
        total_content_height = 130

    total_height = header_height + total_content_height + footer_height + 40

    # Создаем изображение
    img = Image.new("RGB", (card_width, total_height), COLOR_BG)
    draw = ImageDraw.Draw(img)

    # Шрифты
    font_title = _get_font(28, bold=True)
    font_subtitle = _get_font(18, bold=False)
    font_tag = _get_font(14, bold=True)
    font_pair_num = _get_font(16, bold=True)
    font_subj = _get_font(20, bold=True)
    font_detail = _get_font(15, bold=False)
    font_footer = _get_font(13, bold=False)

    # --- 1. ШАПКА ---
    padding_x = 35
    cur_y = 30

    # Бейдж колледжа
    badge_text = "КТМУ • РАСПИСАНИЕ"
    draw.rounded_rectangle([padding_x, cur_y, padding_x + 160, cur_y + 26], radius=6, fill=(30, 58, 138))
    draw.text((padding_x + 12, cur_y + 4), badge_text, font=font_tag, fill=COLOR_ACCENT)

    cur_y += 35
    # Название группы
    draw.text((padding_x, cur_y), f"Группа {group_name}", font=font_title, fill=COLOR_TEXT_WHITE)

    # Дата и неделя
    cur_y += 38
    sub_text = f"{day_name}, {formatted_date}  •  Неделя {week_num} ({parity_str})"
    draw.text((padding_x, cur_y), sub_text, font=font_subtitle, fill=COLOR_TEXT_MUTED)

    cur_y += 45
    # Разделительная линия
    draw.line([(padding_x, cur_y), (card_width - padding_x, cur_y)], fill=COLOR_CARD_BORDER, width=1)
    cur_y += 20

    # --- 2. КАРТОЧКИ ПАР ---
    if not pairs_map:
        # Выходной день
        empty_box = [padding_x, cur_y, card_width - padding_x, cur_y + 90]
        draw.rounded_rectangle(empty_box, radius=12, fill=COLOR_CARD_BG, outline=COLOR_CARD_BORDER, width=1)
        draw.text((padding_x + 30, cur_y + 24), "Занятий нет! Отдыхайте.", font=font_subj, fill=COLOR_GREEN)
        draw.text((padding_x + 30, cur_y + 54), "В этот день пар не запланировано по расписанию колледжа", font=font_detail, fill=COLOR_TEXT_MUTED)
        cur_y += 110
    else:
        for idx, ((p_num, p_time), pair_lessons) in enumerate(pairs_map.items()):
            h = card_heights[idx]
            box = [padding_x, cur_y, card_width - padding_x, cur_y + h]

            # Фон карточки
            draw.rounded_rectangle(box, radius=10, fill=COLOR_CARD_BG, outline=COLOR_CARD_BORDER, width=1)

            # Цветной вертикальный акцент слева
            accent_color = PAIR_COLORS[(p_num - 1) % len(PAIR_COLORS)]
            draw.rounded_rectangle([padding_x, cur_y, padding_x + 6, cur_y + h], radius=3, fill=accent_color)

            # Бейдж номера пары и времени
            time_label = f"{p_num} пара ({p_time})" if p_time else f"{p_num} пара"
            draw.rounded_rectangle([padding_x + 20, cur_y + 14, padding_x + 200, cur_y + 38], radius=6, fill=accent_color)
            draw.text((padding_x + 30, cur_y + 17), time_label, font=font_pair_num, fill=(255, 255, 255))

            if len(pair_lessons) > 1:
                # Деление на подгруппы
                first_subj = pair_lessons[0].get("subject", "Учебное занятие")
                draw.text((padding_x + 215, cur_y + 15), first_subj, font=font_subj, fill=COLOR_TEXT_WHITE)

                sub_y = cur_y + 50
                for s_idx, s_l in enumerate(pair_lessons):
                    sub_num = s_l.get("subgroup", s_idx + 1)
                    room = s_l.get("room", "")
                    teacher = s_l.get("teacher", "")
                    sub_l_type = s_l.get("lesson_type", "")

                    parts = [f"• {sub_num} подгруппа:"]
                    if sub_l_type and sub_l_type != "Занятие":
                        parts.append(f"[{sub_l_type}]")
                    if room:
                        parts.append(f"Ауд. {room}")
                    if teacher:
                        parts.append(f"|  {teacher}")

                    draw.text((padding_x + 30, sub_y), " ".join(parts), font=font_detail, fill=COLOR_TEXT_MUTED)
                    sub_y += 32
            else:
                # Одиночное занятие
                l = pair_lessons[0]
                subject = l.get("subject", "Учебное занятие")
                room = l.get("room", "")
                teacher = l.get("teacher", "")
                fmt = l.get("format", "Очно")
                l_type = l.get("lesson_type", "")

                # Название предмета с типом занятия (если известно)
                subj_title = f"{subject} [{l_type}]" if l_type and l_type != "Занятие" else subject
                draw.text((padding_x + 20, cur_y + 45), subj_title, font=font_subj, fill=COLOR_TEXT_WHITE)

                # Детали (аудитория, преподаватель, формат)
                details = []
                if room:
                    details.append(f"Ауд: {room}")
                if teacher:
                    details.append(f"Преподаватель: {teacher}")
                if fmt and fmt != "Очно":
                    details.append(f"[{fmt}]")

                draw.text((padding_x + 20, cur_y + 75), "   •   ".join(details) if details else "По расписанию", font=font_detail, fill=COLOR_TEXT_MUTED)

            cur_y += h + 12

    # --- 3. ПОДВАЛ ---
    cur_y += 10
    footer_text = f"t.me/{bot_username}  •  Расписание КТМУ"
    draw.text((padding_x, cur_y), footer_text, font=font_footer, fill=(100, 116, 139))

    # Экспорт в память
    output = io.BytesIO()
    img.save(output, format="PNG", quality=95)
    output.seek(0)
    return output
