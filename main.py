import asyncio
import logging
import sys

from aiogram import Bot, Dispatcher
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode

from config import config
from database import db
from handlers import router
from scheduler import setup_scheduler
from structure_parser import sync_structure
from throttling import MenuNavResetMiddleware, ThrottlingMiddleware
from timetable_parser import timetable_parser

# Настройка структурированного логирования
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    handlers=[
        logging.StreamHandler(sys.stdout),
    ],
)
logger = logging.getLogger("ktmu_bot")


# Экземпляр планировщика для graceful shutdown
scheduler_service = None


async def on_startup(bot: Bot) -> None:
    """
    Действия при запуске бота:
    1. Инициализация таблиц базы данных.
    2. Первичная синхронизация структуры колледжа (если групп в базе еще нет или для актуализации).
    3. Запуск планировщика задач APScheduler (рассылка в 20:00 и пары в 07:00).
    4. Первоначальное планирование пар на оставшуюся часть текущего дня.
    """
    global scheduler_service
    logger.info("Запуск Telegram-бота расписания КТМУ...")

    # 1. Инициализация базы данных
    await db.init_db()

    # 2. Синхронизация структуры
    groups_count = await db.count_groups()
    if groups_count == 0:
        logger.info("База данных групп пуста. Запуск первой синхронизации...")
        await sync_structure(db)
    else:
        logger.info("В базе данных уже есть %d групп. Запуск фоновой актуализации...", groups_count)
        asyncio.create_task(sync_structure(db))

    # 3. Инициализация планировщика задач
    scheduler_service = setup_scheduler(bot, db)

    # 4. Если бот запущен днем, сразу планируем оставшиеся пары на сегодня
    asyncio.create_task(scheduler_service.schedule_today_pairs_notifications())

    # 5. Фоновый прогрев расписания для всех активных групп (мгновенная отдача без ожидания)
    asyncio.create_task(timetable_parser.preload_active_groups_schedules(db))

    # 6. Автоматическая регистрация команд в меню Telegram
    from aiogram.types import BotCommand
    commands = [
        BotCommand(command="start", description="Главное меню и запуск"),
        BotCommand(command="today", description="Расписание на сегодня"),
        BotCommand(command="tomorrow", description="Расписание на завтра"),
        BotCommand(command="week", description="Расписание на неделю"),
        BotCommand(command="find_teacher", description="Поиск преподавателя"),
        BotCommand(command="group_help", description="Инструкция по добавлению в чат/тему"),
        BotCommand(command="set_topic", description="Привязать тему форума к группе"),
        BotCommand(command="set_group", description="Выбрать группу (для себя или чата)"),
        BotCommand(command="change_group", description="Сменить группу"),
        BotCommand(command="notifications", description="Настройка уведомлений"),
        BotCommand(command="admin", description="Панель администратора (ID: 870396858)"),
        BotCommand(command="stats", description="Статистика бота (только для админа)"),
        BotCommand(command="author", description="Связь с автором (@yapsychokid)"),
        BotCommand(command="help", description="Справка и помощь"),
    ]
    try:
        await bot.set_my_commands(commands)
        logger.info("Команды меню бота успешно зарегистрированы в Telegram!")
    except Exception as e:
        logger.warning("Не удалось зарегистрировать команды меню бота: %s", e)

    bot_info = await bot.get_me()
    logger.info("Бот @%s успешно авторизован и готов к работе!", bot_info.username)


async def on_shutdown(bot: Bot) -> None:
    """
    Корректная остановка сервисов при завершении процесса (Graceful Shutdown).
    """
    logger.info("Остановка бота...")
    if scheduler_service:
        scheduler_service.shutdown()

    await timetable_parser.close()
    await bot.session.close()
    logger.info("Бот успешно остановлен.")


async def main() -> None:
    """
    Точка входа в приложение.
    """
    if not config.BOT_TOKEN or config.BOT_TOKEN.startswith("YOUR_TELEGRAM"):
        logger.warning(
            "ВНИМАНИЕ: Указан демонстрационный токен BOT_TOKEN в .env! "
            "Для реальной работы с Telegram укажите валидный токен от @BotFather."
        )

    # Инициализация бота с поддержкой HTML форматирования
    bot = Bot(
        token=config.BOT_TOKEN,
        default=DefaultBotProperties(parse_mode=ParseMode.HTML),
    )

    # Инициализация диспетчера
    dp = Dispatcher()
    dp["database"] = db

    # Подключение антиспам-системы (Throttling Middleware)
    throttling = ThrottlingMiddleware(rate_limit=config.THROTTLING_RATE_LIMIT)
    dp.message.outer_middleware(throttling)
    dp.callback_query.outer_middleware(throttling)

    # Авто-сброс FSM-состояний при навигации по меню и командам
    nav_reset = MenuNavResetMiddleware()
    dp.message.outer_middleware(nav_reset)
    dp.callback_query.outer_middleware(nav_reset)

    # Регистрация роутеров
    dp.include_router(router)

    # Регистрация хуков жизненного цикла
    dp.startup.register(on_startup)
    dp.shutdown.register(on_shutdown)

    # Запуск поллинга сообщений
    try:
        await dp.start_polling(bot, allowed_updates=dp.resolve_used_update_types())
    finally:
        await bot.session.close()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except (KeyboardInterrupt, SystemExit):
        logger.info("Бот остановлен пользователем.")
