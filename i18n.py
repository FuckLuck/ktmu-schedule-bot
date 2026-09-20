"""
Модуль интернационализации (RU / EN) для Telegram-бота КТМУ.
Предоставляет локализованные текстовые строки и функции перевода.
"""

from typing import Any

MESSAGES: dict[str, dict[str, str]] = {
    # Приветствие и выбор языка
    "choose_language": {
        "ru": "👋 <b>Добро пожаловать в бот расписания КТМУ!</b>\n\nВыберите язык / Choose language:",
        "en": "👋 <b>Welcome to KTMU Schedule Bot!</b>\n\nChoose language / Выберите язык:",
    },
    "language_selected": {
        "ru": "🇷🇺 Выбран русский язык.",
        "en": "🇬🇧 English language selected.",
    },
    # Напоминание перед парами (Bonch Bot style)
    "lead_time_prompt": {
        "ru": "Могу присылать уведомление о грядущем занятии за несколько минут до него. Если нужно, скажите за сколько:",
        "en": "I can send a reminder before upcoming classes. If needed, please choose how many minutes in advance:",
    },
    "lead_5": {"ru": "⏱ За 5 минут", "en": "⏱ 5 min before"},
    "lead_10": {"ru": "⏱ За 10 минут", "en": "⏱ 10 min before"},
    "lead_15": {"ru": "⏱ За 15 минут", "en": "⏱ 15 min before"},
    "lead_20": {"ru": "⏱ За 20 минут", "en": "⏱ 20 min before"},
    "lead_30": {"ru": "⏱ За 30 минут", "en": "⏱ 30 min before"},
    "lead_45": {"ru": "⏱ За 45 минут", "en": "⏱ 45 min before"},
    "lead_none": {"ru": "❌ Не нужно", "en": "❌ Not needed"},
    "lead_time_saved": {
        "ru": "✅ Настройки уведомлений сохранены! Теперь выберите вашу специальность и группу.",
        "en": "✅ Notification settings saved! Now please select your specialty and group.",
    },

    # Кнопки главного меню (Reply Keyboard)
    "btn_hw": {"ru": "📚 ДЗ 📚", "en": "📚 Homework 📚"},
    "btn_today": {"ru": "📅 Сегодня", "en": "📅 Today"},
    "btn_tomorrow": {"ru": "🌅 Завтра", "en": "🌅 Tomorrow"},
    "btn_week": {"ru": "📆 Вся неделя", "en": "📆 Full Week"},
    "btn_find_teacher": {"ru": "🔍 Преподаватель", "en": "🔍 Teacher"},
    "btn_rooms": {"ru": "🏫 Кабинеты", "en": "🏫 Rooms"},
    "btn_starosta": {"ru": "🙋‍♂️ Староста 🙋‍♂️", "en": "🙋‍♂️ Starosta 🙋‍♂️"},
    "btn_group_menu": {"ru": "🎓 Меню группы (ДЗ / Староста)", "en": "🎓 Group menu (HW / Starosta)"},
    "btn_change_group": {"ru": "⚙️ Сменить группу", "en": "⚙️ Change group"},
    "btn_main_menu": {"ru": "⬅️ Главное меню", "en": "⬅️ Main menu"},
    "btn_settings": {"ru": "⚙️ Настройки", "en": "⚙️ Settings"},
    "btn_lang": {"ru": "🌐 Язык", "en": "🌐 Language"},
    "btn_add_to_group": {"ru": "➕ Добавить бота в группу", "en": "➕ Add bot to group"},
    "btn_now": {"ru": "📍 Где сейчас пара?", "en": "📍 Where is pair now?"},
    "btn_calendar": {"ru": "📅 В календарь (.ics)", "en": "📅 Export to Calendar (.ics)"},
    "btn_skip_pair": {"ru": "💤 Не иду на пару", "en": "💤 Skip pair / Sleep"},
    "btn_my_subgroup": {"ru": "👥 Моя подгруппа", "en": "👥 My subgroup"},

    # Домашнее задание
    "hw_title": {
        "ru": "📚 <b>Домашние задания группы {group_name}</b>\n\n",
        "en": "📚 <b>Homework for group {group_name}</b>\n\n",
    },
    "hw_empty": {
        "ru": "🎉 Заданий пока нет! Отдыхайте.\n\n<i>Староста группы может добавлять домашние задания через меню старосты. Они автоматически транслируются студентам и прикрепляются к расписанию на завтра.</i>",
        "en": "🎉 No homework assigned yet! Enjoy your free time.\n\n<i>The group starosta can add homework via starosta menu. It will automatically broadcast to students and attach to tomorrow's schedule.</i>",
    },
    "hw_broadcast_alert": {
        "ru": "🔔 <b>Новое домашнее задание!</b>\n\n👥 Группа: <b>{group_name}</b>\n📖 Предмет: <b>{subject}</b>\n📅 Срок сдачи: <b>{due_date}</b>\n\n📝 <b>Задание:</b>\n{task_text}\n\n<i>Опубликовал(а): {author}</i>",
        "en": "🔔 <b>New Homework Assignment!</b>\n\n👥 Group: <b>{group_name}</b>\n📖 Subject: <b>{subject}</b>\n📅 Due date: <b>{due_date}</b>\n\n📝 <b>Task:</b>\n{task_text}\n\n<i>Published by: {author}</i>",
    },
    "hw_schedule_section": {
        "ru": "\n\n📚 <b>Домашнее задание на этот день:</b>\n",
        "en": "\n\n📚 <b>Homework for this day:</b>\n",
    },

    # Староста
    "starosta_no_group": {
        "ru": "⚠️ Сначала выберите группу в главном меню!",
        "en": "⚠️ Please select your group first in the main menu!",
    },
    "starosta_none": {
        "ru": "У вашей группы нет старосты.\n\nВы можете подать заявку на статус старосты. Назначение подтверждается администратором бота.",
        "en": "Your group has no starosta.\n\nYou can apply for starosta status. Appointment must be confirmed by the bot administrator.",
    },
    "btn_claim_starosta": {
        "ru": "🙋‍♂️ Подать заявку на старосту 🙋‍♂️",
        "en": "🙋‍♂️ Apply for Starosta 🙋‍♂️",
    },
    "starosta_request_sent": {
        "ru": "📨 <b>Заявка отправлена!</b>\n\nВаша заявка на пост старосты группы <b>{group_name}</b> передана главному администратору бота. Как только она будет подтверждена, вам придет уведомление!",
        "en": "📨 <b>Application sent!</b>\n\nYour starosta application for group <b>{group_name}</b> has been sent to the administrator. You will be notified once approved!",
    },
    "starosta_request_pending": {
        "ru": "⏳ Ваша заявка на статус старосты группы <b>{group_name}</b> уже ожидает решения администратора.",
        "en": "⏳ Your starosta application for group <b>{group_name}</b> is already pending administrator decision.",
    },
    "starosta_claimed_success": {
        "ru": "🎉 Поздравляем! Вы назначены старостой группы <b>{group_name}</b>.\nТеперь вам доступно добавление и управление домашними заданиями.",
        "en": "🎉 Congratulations! You are now the starosta of group <b>{group_name}</b>.\nYou can now add and manage homework assignments.",
    },
    "starosta_approved_notify": {
        "ru": (
            "🎉 <b>Поздравляем! Администратор подтвердил ваш статус старосты группы {group_name}!</b>\n\n"
            "📋 <b>Что вам нужно сделать (памятка старосты):</b>\n"
            "1️⃣ <b>Привяжите чат группы:</b> в меню группы нажмите «💬 Чат группы» и отправьте ссылку на вашу официальную беседу (Telegram или VK), чтобы одногруппники могли находить её в один клик.\n"
            "2️⃣ <b>Публикуйте домашние задания:</b> в меню «📚 ДЗ» выкладывайте домашние задания — бот автоматически разошлет их всем студентам группы!\n"
            "3️⃣ <b>Назначьте заместителя:</b> в меню «🙋‍♂️ Староста» вы можете добавить одного зама (он тоже сможет писать ДЗ и обновлять чат группы)."
        ),
        "en": (
            "🎉 <b>Congratulations! The administrator approved your status as starosta of group {group_name}!</b>\n\n"
            "📋 <b>Starosta Guidelines:</b>\n"
            "1️⃣ <b>Set group chat link:</b> open «💬 Group chat» and paste the link to your official chat.\n"
            "2️⃣ <b>Post homework:</b> open «📚 Homework» to publish assignments with automated broadcasts.\n"
            "3️⃣ <b>Appoint deputy:</b> in «🙋‍♂️ Starosta» you can appoint 1 deputy to help you manage homework and links."
        ),
    },
    "starosta_rejected_notify": {
        "ru": "❌ Ваша заявка на статус старосты группы <b>{group_name}</b> была отклонена администратором.",
        "en": "❌ Your application for starosta of group <b>{group_name}</b> was rejected by the administrator.",
    },
    "starosta_info": {
        "ru": "👑 <b>Староста группы {group_name}:</b>\n👤 {name} ({username})\n📅 Назначен(а): {date}",
        "en": "👑 <b>Starosta of group {group_name}:</b>\n👤 {name} ({username})\n📅 Appointed: {date}",
    },
    "starosta_you_are": {
        "ru": "\n\n<i>Вы являетесь старостой этой группы. Используйте кнопки ниже для управления домашними заданиями:</i>",
        "en": "\n\n<i>You are the starosta of this group. Use the buttons below to manage homework:</i>",
    },
    "btn_add_hw": {"ru": "✏️ Добавить ДЗ", "en": "✏️ Add Homework"},
    "btn_delete_hw": {"ru": "🗑 Удалить ДЗ", "en": "🗑 Delete Homework"},
    "btn_resign_starosta": {"ru": "📨 Запрос на снятие старосты", "en": "📨 Request resignation"},
    "starosta_resign_request_sent": {
        "ru": "📨 <b>Запрос отправлен!</b>\n\nЗапрос на сложение полномочий старосты группы <b>{group_name}</b> направлен главному администратору бота.",
        "en": "📨 <b>Request sent!</b>\n\nResignation request for starosta of group <b>{group_name}</b> sent to the administrator.",
    },
    "starosta_resigned": {
        "ru": "Вы сложили полномочия старосты группы.",
        "en": "You have resigned as group starosta.",
    },

    # Создание ДЗ
    "hw_prompt_subject": {
        "ru": "Выберите предмет из списка занятий вашей группы или напишите название предмета сообщением:",
        "en": "Select a subject from your group's classes or type the subject name:",
    },
    "hw_prompt_due_date": {
        "ru": "Выберите срок сдачи или введите дату в формате <code>ДД.ММ.ГГГГ</code> (например, <code>15.09.2026</code>):",
        "en": "Select the due date or enter it as <code>DD.MM.YYYY</code> (e.g. <code>15.09.2026</code>):",
    },
    "hw_prompt_text": {
        "ru": "Введите текст домашнего задания (номера задач, параграфы, ссылка и т.д.):",
        "en": "Enter the homework task details (exercise numbers, pages, links, etc.):",
    },
    "hw_saved_and_broadcast": {
        "ru": "✅ Домашнее задание по предмету <b>{subject}</b> на <b>{due_date}</b> успешно сохранено и отправлено всем студентам вашей группы ({count} получателей)!",
        "en": "✅ Homework for <b>{subject}</b> on <b>{due_date}</b> saved and broadcast to all group students ({count} recipients)!",
    },
    "hw_cancelled": {
        "ru": "❌ Добавление домашнего задания отменено.",
        "en": "❌ Homework creation cancelled.",
    },

    # Звонки (Расписание звонков)
    "bells_title": {
        "ru": "⏰ <b>Расписание звонков колледжа</b>\n\n",
        "en": "⏰ <b>College Bell Timetable</b>\n\n",
    },
    "bells_card": {
        "ru": "1️⃣ пара: <b>08:30 – 10:00</b>  <i>(перемена 10 мин)</i>\n2️⃣ пара: <b>10:10 – 11:40</b>  <i>(перемена 10 мин)</i>\n3️⃣ пара: <b>11:50 – 13:20</b>  <i>(🍽 большая перемена 40 мин)</i>\n4️⃣ пара: <b>14:00 – 15:30</b>  <i>(перемена 10 мин)</i>\n5️⃣ пара: <b>15:40 – 17:10</b>  <i>(перемена 10 мин)</i>\n6️⃣ пара: <b>17:20 – 18:50</b>  <i>(перемена 10 мин)</i>\n7️⃣ пара: <b>19:00 – 20:30</b>\n\n",
        "en": "1️⃣ class: <b>08:30 – 10:00</b>  <i>(break 10 min)</i>\n2️⃣ class: <b>10:10 – 11:40</b>  <i>(break 10 min)</i>\n3️⃣ class: <b>11:50 – 13:20</b>  <i>(🍽 big break / lunch 40 min)</i>\n4️⃣ class: <b>14:00 – 15:30</b>  <i>(break 10 min)</i>\n5️⃣ class: <b>15:40 – 17:10</b>  <i>(break 10 min)</i>\n6️⃣ class: <b>17:20 – 18:50</b>  <i>(break 10 min)</i>\n7️⃣ class: <b>19:00 – 20:30</b>\n\n",
    },

    # Чат группы (Ссылка на беседу)
    "group_chat_title": {
        "ru": "💬 <b>Официальная беседа группы {group_name}</b>\n\n",
        "en": "💬 <b>Official group chat: {group_name}</b>\n\n",
    },
    "group_chat_none": {
        "ru": "Ссылка на беседу группы еще не добавлена.\nСтароста группы может указать ссылку на чат Telegram или VK по кнопке ниже.",
        "en": "No group chat link added yet.\nGroup starosta can add a Telegram or VK chat link using the button below.",
    },
    "group_chat_prompt_url": {
        "ru": "Отправьте ссылку на беседу группы (например: <code>https://t.me/+...</code> или <code>https://vk.me/join/...</code>):",
        "en": "Send the group chat link (e.g. <code>https://t.me/+...</code> or <code>https://vk.me/join/...</code>):",
    },
    "group_chat_saved": {
        "ru": "✅ Ссылка на беседу группы успешно сохранена!",
        "en": "✅ Group chat link saved successfully!",
    },
    "group_chat_deleted": {
        "ru": "🗑 Ссылка на беседу группы удалена.",
        "en": "🗑 Group chat link removed.",
    },

    # Корпуса и кабинеты
    "campus_title": {
        "ru": "🏢 <b>Навигатор по колледжу КТМУ</b>\n\nВыберите этаж или воспользуйтесь поиском аудитории:",
        "en": "🏢 <b>KTMU Campus Navigator</b>\n\nSelect a floor or search for a specific room:",
    },
    "campus_search_prompt": {
        "ru": "Введите номер кабинета или название помещения (например: <code>304</code>, <code>спортзал</code>, <code>библиотека</code>, <code>деканат</code>):",
        "en": "Enter classroom number or room name (e.g. <code>304</code>, <code>gym</code>, <code>library</code>, <code>dean</code>):",
    },

    # Мои преподаватели
    "my_teachers_title": {
        "ru": "🌟 <b>Преподаватели группы {group_name}</b>\n\nСписок преподавателей по текущему расписанию:\n",
        "en": "🌟 <b>Teachers of group {group_name}</b>\n\nTeachers list according to current timetable:\n",
    },
    "my_teachers_empty": {
        "ru": "⚠️ В текущем расписании группы преподаватели пока не указаны.",
        "en": "⚠️ No teachers found in current group schedule.",
    },

    # Сессия / Экзамены
    "exams_title": {
        "ru": "📅 <b>Расписание экзаменов и сессии группы {group_name}</b>\n\n",
        "en": "📅 <b>Exams and Session Schedule for {group_name}</b>\n\n",
    },
    "exams_empty": {
        "ru": "🎉 Экзаменов и зачетов пока не назначено!\n\n<i>Староста группы может внести даты зачетов, экзаменов, консультаций и пересдач.</i>",
        "en": "🎉 No exams or tests scheduled yet!\n\n<i>Group starosta can add dates for exams, tests, and consultations.</i>",
    },
    "exams_saved": {
        "ru": "✅ Экзамен/зачет по предмету <b>{subject}</b> успешно сохранен!",
        "en": "✅ Exam/test for <b>{subject}</b> saved successfully!",
    },
    "exams_deleted": {
        "ru": "🗑 Экзамен успешно удален.",
        "en": "🗑 Exam deleted successfully.",
    },

    # Личные заметки / Дедлайны
    "notes_title": {
        "ru": "📝 <b>Ваши личные заметки и дедлайны</b>\n\nПерсональный блокнот (виден только вам):\n",
        "en": "📝 <b>Your Personal Notes and Deadlines</b>\n\nPrivate notepad (visible only to you):\n",
    },
    "notes_empty": {
        "ru": "📝 У вас пока нет сохраненных заметок.\nНажмите <b>➕ Добавить заметку</b>, чтобы сохранить личные долги, дедлайны или напоминания!",
        "en": "📝 You have no notes saved yet.\nTap <b>➕ Add note</b> to store your personal tasks, deadlines, or reminders!",
    },
    "notes_prompt_text": {
        "ru": "Введите текст заметки (например: <i>Сдать реферат по истории до 20 сентября</i>):",
        "en": "Enter note text (e.g. <i>Submit history essay by Sep 20</i>):",
    },
    "notes_saved": {
        "ru": "✅ Заметка успешно сохранена в вашем блокноте!",
        "en": "✅ Note saved to your personal notepad!",
    },
    "notes_deleted": {
        "ru": "🗑 Заметка успешно удалена.",
        "en": "🗑 Note removed.",
    },

    # Настройки уведомлений
    "notif_settings_title": {
        "ru": "🔔 <b>Настройки времени рассылок и уведомлений</b>\n\nЗдесь вы можете гибко настроить время автоматической отправки расписания и напоминаний перед парами:",
        "en": "🔔 <b>Notification and Schedule Dispatch Settings</b>\n\nConfigure custom times for schedule broadcasts and class reminders:",
    },
}



def get_text(key: str, lang: str = "ru", **kwargs: Any) -> str:
    """
    Возвращает локализованную строку по ключу и языку.
    Поддерживает форматирование через kwargs.
    """
    if lang not in ("ru", "en"):
        lang = "ru"

    entry = MESSAGES.get(key)
    if not entry:
        return key

    text = entry.get(lang) or entry.get("ru", key)
    if kwargs:
        try:
            return text.format(**kwargs)
        except Exception:
            return text
    return text
