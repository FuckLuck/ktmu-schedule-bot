import asyncio
import logging
from datetime import date, datetime, time, timedelta
from typing import Any
from zoneinfo import ZoneInfo

from aiogram import Bot
from aiogram.exceptions import TelegramBadRequest, TelegramForbiddenError
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger
from apscheduler.triggers.date import DateTrigger

from config import config
from database import Database, db as default_db
from timetable_parser import (
    format_day_schedule_message,
    format_pair_notification,
    timetable_parser,
)

logger = logging.getLogger(__name__)


class NotificationScheduler:
    """
    Планировщик периодических рассылок и точечных уведомлений через APScheduler:
    1. 20:00 — Ежедневная вечерняя рассылка расписания на завтра с группировкой по group_id.
    2. 07:00 — Утреннее создание точечных задач на отправку уведомлений за 0 минут до начала каждой пары.
    3. Обработка TelegramForbiddenError для автоматического отключения уведомлений заблокировавшим бота пользователям.
    """

    def __init__(self, bot: Bot, database: Database = default_db):
        self.bot = bot
        self.db = database
        self.tz = ZoneInfo(config.TIMEZONE)
        self.scheduler = AsyncIOScheduler(timezone=self.tz)

    def get_current_date(self) -> date:
        return datetime.now(self.tz).date()

    async def _safe_send_message(self, user_id: int, text: str) -> bool:
        """
        Безопасная отправка сообщения пользователю с перехватом блокировки бота.
        Если бот заблокирован, отключает notifications_enabled в базе данных.
        """
        try:
            await self.bot.send_message(
                chat_id=user_id,
                text=text,
                parse_mode="HTML",
                disable_web_page_preview=True
            )
            return True
        except TelegramForbiddenError:
            logger.info("Пользователь %d заблокировал бота. Отключаем уведомления в БД.", user_id)
            await self.db.set_user_notifications(user_id, False)
            return False
        except TelegramBadRequest as e:
            if "chat not found" in str(e).lower() or "user is deactivated" in str(e).lower():
                logger.info("Чат с пользователем %d не найден / деактивирован. Отключаем уведомления.", user_id)
                await self.db.set_user_notifications(user_id, False)
            else:
                logger.warning("Ошибка TelegramBadRequest при отправке пользователю %d: %s", user_id, e)
            return False
        except Exception as e:
            logger.error("Непредвиденная ошибка отправки сообщения пользователю %d: %s", user_id, e)
            return False

    # -------------------------------------------------------------------------
    # ЗАДАЧА 1: ЕЖЕДНЕВНАЯ РАССЫЛКА В 20:00 (НА ЗАВТРА)
    # -------------------------------------------------------------------------

    async def broadcast_tomorrow_schedule(self) -> None:
        """
        В 20:00 (8 часов вечера): Ежедневная рассылка расписания на завтра.
        Группирует пользователей по group_id, делая 1 парсинг на группу.
        """
        logger.info("Запуск вечерней рассылки расписания на завтра (20:00)...")
        grouped_users = await self.db.get_active_users_grouped_by_group()

        if not grouped_users:
            logger.info("Нет активных пользователей с включенными уведомлениями.")
            return

        tomorrow = self.get_current_date() + timedelta(days=1)
        logger.info("Рассылка на завтра (%s) для %d уникальных групп...", tomorrow, len(grouped_users))

        total_sent = 0
        for group_id, user_ids in grouped_users.items():
            try:
                group = await self.db.get_group_by_id(group_id)
                group_name = group["group_name"] if group else group_id
                group_url = group["relative_url"] if group else ""

                # Ровно 1 парсинг на группу
                sched = await timetable_parser.fetch_day_schedule(
                    group_id=group_id,
                    group_url=group_url,
                    target_date=tomorrow,
                    force_refresh=True  # Обновляем кэш свежими данными
                )

                header = "📢 <b>Вечерняя рассылка расписания на завтра (20:00):</b>\n\n"
                message_text = header + format_day_schedule_message(sched, group_name)

                # Рассылка всем студентам данной группы
                for uid in user_ids:
                    success = await self._safe_send_message(uid, message_text)
                    if success:
                        total_sent += 1
                    await asyncio.sleep(0.04)

            except Exception as e:
                logger.error("Ошибка рассылки для группы %s: %s", group_id, e, exc_info=True)

        logger.info("Вечерняя рассылка завершена. Доставлено сообщений: %d", total_sent)

    # -------------------------------------------------------------------------
    # ЗАДАЧА 1.1: ЕЖЕДНЕВНАЯ УТРЕННЯЯ РАССЫЛКА В 08:00 (НА СЕГОДНЯ)
    # -------------------------------------------------------------------------

    async def broadcast_today_morning_schedule(self) -> None:
        """
        В 08:00 (8 часов утра): Утренняя рассылка расписания на сегодня.
        Группирует пользователей по group_id, делая 1 парсинг на группу.
        """
        logger.info("Запуск утренней рассылки расписания на сегодня (08:00)...")
        grouped_users = await self.db.get_active_users_grouped_by_group()

        if not grouped_users:
            return

        today = self.get_current_date()
        total_sent = 0
        for group_id, user_ids in grouped_users.items():
            try:
                group = await self.db.get_group_by_id(group_id)
                group_name = group["group_name"] if group else group_id
                group_url = group["relative_url"] if group else ""

                sched = await timetable_parser.fetch_day_schedule(
                    group_id=group_id,
                    group_url=group_url,
                    target_date=today,
                )

                header = "🌅 <b>Доброе утро! Расписание на сегодня (08:00):</b>\n\n"
                message_text = header + format_day_schedule_message(sched, group_name)

                for uid in user_ids:
                    success = await self._safe_send_message(uid, message_text)
                    if success:
                        total_sent += 1
                    await asyncio.sleep(0.04)
            except Exception as e:
                logger.error("Ошибка утренней рассылки для группы %s: %s", group_id, e, exc_info=True)

        logger.info("Утренняя рассылка 08:00 завершена. Доставлено сообщений: %d", total_sent)

    # -------------------------------------------------------------------------
    # ЗАДАЧА 2: В 07:00 УТРЕННЕЕ ПЛАНИРОВАНИЕ ТОЧЕЧНЫХ НАПОМИНАНИЙ О ПАРАХ
    # -------------------------------------------------------------------------

    async def schedule_today_pairs_notifications(self) -> None:
        """
        В 07:00: Анализирует расписание на сегодня для всех групп с активными студентами
        и создает точечные задачи APScheduler за 0 минут до начала каждой пары.
        """
        today = self.get_current_date()
        now = datetime.now(self.tz)
        logger.info("Запуск утреннего планирования уведомлений о начале пар (07:00, дата: %s)...", today)

        grouped_users = await self.db.get_active_users_grouped_by_group()
        if not grouped_users:
            logger.info("Нет активных пользователей для напоминаний о парах.")
            return

        scheduled_jobs_count = 0

        for group_id, user_ids in grouped_users.items():
            try:
                group = await self.db.get_group_by_id(group_id)
                group_name = group["group_name"] if group else group_id
                group_url = group["relative_url"] if group else ""

                sched = await timetable_parser.fetch_day_schedule(
                    group_id=group_id,
                    group_url=group_url,
                    target_date=today,
                )

                lessons = sched.get("lessons", [])
                if not lessons:
                    continue

                # Группируем уроки по номеру пары / времени начала
                for lesson in lessons:
                    start_time_str = lesson.get("start_time", "").strip()
                    if not start_time_str:
                        continue

                    # Парсим время старта (например "08:30")
                    try:
                        time_parts = [int(p) for p in start_time_str.split(":")]
                        pair_time = time(hour=time_parts[0], minute=time_parts[1])
                        pair_datetime = datetime.combine(today, pair_time, tzinfo=self.tz)
                    except Exception as ex:
                        logger.warning("Не удалось разобрать время начала пары '%s': %s", start_time_str, ex)
                        continue

                    # Если пара еще не началась в текущий день
                    if pair_datetime > now:
                        job_id = f"pair_{group_id}_{today}_{lesson.get('pair_number')}_{start_time_str}"

                        # Создаем точечную задачу ровно за 0 минут до звонка
                        self.scheduler.add_job(
                            self._send_pair_start_alert,
                            trigger=DateTrigger(run_date=pair_datetime, timezone=self.tz),
                            id=job_id,
                            replace_existing=True,
                            kwargs={
                                "group_id": group_id,
                                "group_name": group_name,
                                "lesson": lesson,
                            }
                        )
                        scheduled_jobs_count += 1
                        logger.debug("Запланировано уведомление для %s на %s", group_name, pair_datetime)

            except Exception as e:
                logger.error("Ошибка планирования пар для группы %s: %s", group_id, e, exc_info=True)

        logger.info("Утреннее планирование завершено. Запланировано точечных напоминаний: %d", scheduled_jobs_count)

    async def _send_pair_start_alert(self, group_id: str, group_name: str, lesson: dict[str, Any]) -> None:
        """
        Точечный триггер: отправляет уведомление о начале пары студентам группы.
        """
        logger.info("Отправка напоминания о начале пары для группы %s (%s)", group_name, lesson.get("subject"))

        # Получаем свежий список активных пользователей группы
        grouped_users = await self.db.get_active_users_grouped_by_group()
        user_ids = grouped_users.get(group_id, [])

        if not user_ids:
            return

        text = format_pair_notification(lesson, group_name)

        for uid in user_ids:
            await self._safe_send_message(uid, text)
            await asyncio.sleep(0.04)

    # -------------------------------------------------------------------------
    # ИНИЦИАЛИЗАЦИЯ И СТАРТ
    # -------------------------------------------------------------------------

    def start(self) -> None:
        """
        Запускает крон-задачи планировщика в соответствии с ТЗ.
        """
        # 1. Рассылка расписания на завтра в 20:00 каждый день
        self.scheduler.add_job(
            self.broadcast_tomorrow_schedule,
            trigger=CronTrigger(hour=20, minute=0, timezone=self.tz),
            id="daily_broadcast_20_00",
            replace_existing=True,
            misfire_grace_time=300
        )

        # 2. Утреннее планирование точечных напоминаний о парах в 07:00 каждый день
        self.scheduler.add_job(
            self.schedule_today_pairs_notifications,
            trigger=CronTrigger(hour=7, minute=0, timezone=self.tz),
            id="daily_morning_pairs_07_00",
            replace_existing=True,
            misfire_grace_time=300
        )

        # 3. Утренняя рассылка расписания на сегодня в 08:00 каждый день
        self.scheduler.add_job(
            self.broadcast_today_morning_schedule,
            trigger=CronTrigger(hour=8, minute=0, timezone=self.tz),
            id="daily_morning_broadcast_08_00",
            replace_existing=True,
            misfire_grace_time=300
        )

        self.scheduler.start()
        logger.info("APScheduler успешно запущен: настроены крон-задачи на 20:00 и 07:00 (%s)", config.TIMEZONE)

    def shutdown(self) -> None:
        """
        Останавливает планировщик при завершении работы бота.
        """
        if self.scheduler.running:
            self.scheduler.shutdown(wait=False)
            logger.info("APScheduler остановлен.")


def setup_scheduler(bot: Bot, database: Database = default_db) -> NotificationScheduler:
    """
    Фабрика для создания и запуска планировщика уведомлений.
    """
    scheduler_service = NotificationScheduler(bot=bot, database=database)
    scheduler_service.start()
    return scheduler_service
