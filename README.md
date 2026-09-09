# 🎓 Telegram-бот расписания колледжа КТМУ

[![Python](https://img.shields.io/badge/Python-3.11%20%7C%203.12-blue?logo=python&logoColor=white)](https://www.python.org/)
[![aiogram](https://img.shields.io/badge/aiogram-3.x-2CA5E0?logo=telegram&logoColor=white)](https://github.com/aiogram/aiogram)
[![SQLite](https://img.shields.io/badge/SQLite-aiosqlite-003B57?logo=sqlite&logoColor=white)](https://sqlite.org/)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](https://opensource.org/licenses/MIT)
[![Author](https://img.shields.io/badge/Author-@yapsychokid-informational?logo=telegram&logoColor=white)](https://t.me/yapsychokid)

Асинхронный Telegram-бот для студентов колледжа КТМУ с поддержкой **всех специальностей и групп** с сайта [timetable-ktmu.ru](https://timetable-ktmu.ru/).

---

## ✨ Ключевые возможности

- 🚀 **Пошаговый выбор группы**: Интуитивный выбор специальности и группы на Inline-кнопках без лишних переходов.
- 🔄 **Кнопки «Назад» везде**: Удобная навигация между группами, специальностями и главным меню.
- 🕒 **Умное переключение расписания**:
  - Если пары на сегодня уже закончились (например, время 16:00, а последняя пара завершилась в 15:30), бот **автоматически показывает расписание на завтра (на четверг)** с возможностью в 1 клик посмотреть прошедшие занятия за сегодня.
- 🔔 **Интеллектуальные рассылки (APScheduler)**:
  - **В 20:00 (вечер)**: рассылка расписания на завтра (1 запрос к сайту на группу).
  - **В 08:00 (утро)**: утренняя рассылка расписания на сегодня.
  - **В 07:00**: планирование точечных напоминаний ровно за 0 минут до начала каждой пары.
  - **Автоматическая обработка блокировок**: при блокировке бота уведомления для пользователя корректно деактивируются (`TelegramForbiddenError`).
- ⚡ **Многоуровневое кэширование**: Таблица `timetable_cache` SQLite снижает нагрузку на сайт колледжа.
- 👨‍💻 **Кнопка связи с автором**: Прямой переход в диалог с разработчиком ([@yapsychokid](https://t.me/yapsychokid)) прямо из главного меню бота.

---

## 🛠 Технический стек

- **Python 3.12+**
- **aiogram 3.31+** (асинхронный фреймворк Telegram Bot API)
- **aiohttp** & **BeautifulSoup4** (гибридный парсер: HTML + JSON API хранилища)
- **aiosqlite** (асинхронная база данных SQLite)
- **APScheduler** (планировщик периодических задач и точечных напоминаний)
- **pydantic-settings** (типизированная конфигурация)
- **Docker** & **Docker Compose** (контейнеризация для быстрого продакшн-деплоя)

---

## 🗄 Структура базы данных (SQLite)

```sql
-- Пользователи
CREATE TABLE IF NOT EXISTS users (
    user_id INTEGER PRIMARY KEY,
    group_id TEXT,
    group_url TEXT,
    group_name TEXT,
    notifications_enabled BOOLEAN DEFAULT 1
);

-- Группы и специальности
CREATE TABLE IF NOT EXISTS groups (
    id TEXT PRIMARY KEY,
    specialty_name TEXT NOT NULL,
    group_name TEXT NOT NULL,
    relative_url TEXT NOT NULL
);

-- Кэш расписания
CREATE TABLE IF NOT EXISTS timetable_cache (
    group_id TEXT NOT NULL,
    date TEXT NOT NULL,
    data_json TEXT NOT NULL,
    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY (group_id, date)
);
```

---

## 🚀 Быстрый запуск

### Вариант 1: Локальный запуск

1. **Клонируйте репозиторий:**
   ```bash
   git clone https://github.com/FuckLuck/ktmu-schedule-bot.git
   cd ktmu-schedule-bot
   ```

2. **Создайте виртуальное окружение и установите зависимости:**
   ```bash
   python -m venv venv
   # Windows:
   .\venv\Scripts\activate
   # Linux/macOS:
   source venv/bin/activate

   pip install -r requirements.txt
   ```

3. **Настройте переменные окружения:**
   Скопируйте шаблон `.env.example` в `.env`:
   ```bash
   cp .env.example .env
   ```
   Укажите ваш токен от [@BotFather](https://t.me/BotFather):
   ```env
   BOT_TOKEN=ВАШ_ТОКЕН_БОТА
   BASE_URL=https://timetable-ktmu.ru
   DATABASE_PATH=ktmu_bot.db
   TIMEZONE=Europe/Moscow
   CACHE_TTL_HOURS=6
   ```

4. **Запустите тесты:**
   ```bash
   pytest test_bot.py -v
   ```

5. **Запустите бота:**
   ```bash
   python main.py
   ```

---

### Вариант 2: Запуск через Docker Compose

```bash
docker compose up -d --build
```

---

## 📋 Команды бота (@BotFather /setcommands)

Команды автоматически регистрируются в Telegram при первом запуске, либо их можно настроить вручную через [@BotFather](https://t.me/BotFather):

```
start - Главное меню и запуск
today - Расписание на сегодня
tomorrow - Расписание на завтра
week - Расписание на неделю
change_group - Сменить группу
notifications - Настройка уведомлений
author - Связь с автором (@yapsychokid)
help - Справка и помощь
```

---

## 👨‍💻 Автор и поддержка

- Разработчик: **[@yapsychokid](https://t.me/yapsychokid)**
- Репозиторий: [FuckLuck/ktmu-schedule-bot](https://github.com/FuckLuck/ktmu-schedule-bot)
- По всем вопросам, баг-репортам и предложениям обращайтесь в Telegram: [@yapsychokid](https://t.me/yapsychokid)

---

## 📄 Лицензия

Проект распространяется под свободной лицензией [MIT](LICENSE).