import asyncio
import logging
import os
from datetime import date, datetime, time, timedelta
from typing import Any, Optional
from zoneinfo import ZoneInfo

import aiosqlite

from aiogram import Bot
from aiogram.exceptions import TelegramBadRequest, TelegramForbiddenError
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger
from apscheduler.triggers.date import DateTrigger

from config import config
from database import Database, db as default_db
from timetable_parser import (
    compute_schedule_hash,
    format_day_schedule_message,
    format_pair_notification,
    timetable_parser,
)

logger = logging.getLogger(__name__)


class NotificationScheduler:
    """
    Планировщик периодических рассылок и точечных уведомлений через APScheduler:
    1. 20:00 — Ежедневная вечерняя рассылка расписания на завтра (в ЛС и привязанные темы групп).
    2. 08:00 — Ежедневная утренняя рассылка расписания на сегодня.
    3. 07:00 — Утреннее планирование точечных напоминаний о начале пар в точное время начала звонка.
    4. Каждые 15 минут — Мониторинг изменений и замен расписания по хэшам (hash) с оповещением.
    5. Корректная передача message_thread_id во все отправки сообщений в темы.
    """

    def __init__(self, bot: Bot, database: Database = default_db):
        self.bot = bot
        self.db = database
        self.tz = ZoneInfo(config.TIMEZONE)
        self.scheduler = AsyncIOScheduler(timezone=self.tz)

    def get_current_date(self) -> date:
        return datetime.now(self.tz).date()

    async def _safe_send_message(
        self, chat_id: int, text: str, message_thread_id: Optional[int] = None
    ) -> bool:
        """
        Безопасная отправка сообщения пользователю или в тему чата с перехватом блокировки бота.
        Если бот заблокирован, отключает уведомления в базе данных.
        """
        try:
            kwargs: dict[str, Any] = {
                "chat_id": chat_id,
                "text": text,
                "parse_mode": "HTML",
                "disable_web_page_preview": True,
            }
            if message_thread_id is not None:
                kwargs["message_thread_id"] = message_thread_id

            await self.bot.send_message(**kwargs)
            return True
        except TelegramForbiddenError:
            logger.info("Бот заблокирован в чате %d (thread %s). Отключаем уведомления.", chat_id, message_thread_id)
            if chat_id < 0:
                await self.db.set_chat_notifications(chat_id, message_thread_id, False)
            else:
                await self.db.set_user_notifications(chat_id, False)
            return False
        except TelegramBadRequest as e:
            err_str = str(e).lower()
            if any(k in err_str for k in ("chat not found", "user is deactivated", "thread not found", "topic_closed", "topic_deleted")):
                logger.info("Чат/тема %d (thread %s) недоступен (%s). Отключаем уведомления.", chat_id, message_thread_id, e)
                if chat_id < 0:
                    await self.db.set_chat_notifications(chat_id, message_thread_id, False)
                else:
                    await self.db.set_user_notifications(chat_id, False)
            else:
                logger.warning("Ошибка TelegramBadRequest при отправке в чат %d (thread %s): %s", chat_id, message_thread_id, e)
            return False
        except Exception as e:
            logger.error("Непредвиденная ошибка отправки сообщения в чат %d (thread %s): %s", chat_id, message_thread_id, e)
            return False

    async def _get_all_subscribers_by_group(self) -> dict[str, list[tuple[int, Optional[int]]]]:
        """
        Возвращает словарь {group_id: [(chat_id, message_thread_id), ...]}
        для всех пользователей ЛС (thread_id=None) и привязанных групп/тем форума.
        """
        result: dict[str, list[tuple[int, Optional[int]]]] = {}

        # 1. Личные чаты пользователей
        users_by_group = await self.db.get_active_users_grouped_by_group()
        for gid, uids in users_by_group.items():
            for uid in uids:
                result.setdefault(gid, []).append((uid, None))

        # 2. Групповые чаты и темы форумов
        chats_by_group = await self.db.get_active_chats_grouped_by_group()
        for gid, chat_tuples in chats_by_group.items():
            for cid, mtid in chat_tuples:
                result.setdefault(gid, []).append((cid, mtid))

        return result

    async def _get_subscribers_with_lead_by_group(self) -> dict[str, dict[int, list[tuple[int, Optional[int]]]]]:
        """
        Возвращает словарь {group_id: {lead_minutes: [(chat_id, message_thread_id), ...]}}
        для точечных напоминаний с учетом индивидуального интервала (5, 10, 15... минут).
        """
        result: dict[str, dict[int, list[tuple[int, Optional[int]]]]] = {}
        async with aiosqlite.connect(self.db.db_path) as db:
            db.row_factory = aiosqlite.Row
            async with db.execute(
                "SELECT user_id, group_id, notify_lead_minutes FROM users WHERE notifications_enabled = 1 AND group_id IS NOT NULL"
            ) as cur:
                for r in await cur.fetchall():
                    gid = r["group_id"]
                    lead = int(r["notify_lead_minutes"] or 0)
                    result.setdefault(gid, {}).setdefault(lead, []).append((r["user_id"], None))

            async with db.execute(
                "SELECT chat_id, message_thread_id, group_id, notify_lead_minutes FROM chats WHERE notifications_enabled = 1 AND group_id IS NOT NULL"
            ) as cur:
                for r in await cur.fetchall():
                    gid = r["group_id"]
                    lead = int(r["notify_lead_minutes"] or 0)
                    result.setdefault(gid, {}).setdefault(lead, []).append((r["chat_id"], r["message_thread_id"]))
        return result

    # -------------------------------------------------------------------------
    # ЗАДАЧА 1: ЕЖЕДНЕВНАЯ ВЕЧЕРНЯЯ РАССЫЛКА (НА ЗАВТРА) ПО СЛОТАМ
    # -------------------------------------------------------------------------

    async def broadcast_tomorrow_schedule(self, time_slot: str = "20:00") -> None:
        """
        Ежедневная рассылка расписания на завтра для выбранного временного слота (по умолчанию 20:00).
        Группирует получателей по group_id, делая 1 парсинг на группу.
        Рассылает в ЛС пользователей и в привязанные темы групп.
        Также автоматически включает актуальное домашнее задание на завтра!
        """
        logger.info("Запуск вечерней рассылки расписания на завтра (слот %s)...", time_slot)
        subscribers_map = await self.db.get_subscribers_for_evening_slot(time_slot)

        if not subscribers_map:
            logger.info("Нет активных пользователей или чатов для вечернего слота %s.", time_slot)
            return

        tomorrow = self.get_current_date() + timedelta(days=1)
        logger.info("Вечерняя рассылка на завтра (%s, слот %s) для %d уникальных групп...", tomorrow, time_slot, len(subscribers_map))

        total_sent = 0
        for group_id, subscribers in subscribers_map.items():
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

                header = f"📢 <b>Вечерняя рассылка расписания на завтра ({time_slot}):</b>\n\n"
                message_text = header + format_day_schedule_message(sched, group_name)

                # Добавляем домашнее задание на завтра при наличии
                tomorrow_fmt = tomorrow.strftime("%d.%m.%Y")
                hw_items = await self.db.get_homework_for_date(group_id, tomorrow_fmt)
                if not hw_items:
                    hw_items = await self.db.get_homework_for_date(group_id, tomorrow.isoformat())
                if hw_items:
                    message_text += "\n\n📚 <b>Домашнее задание на завтра:</b>\n"
                    for idx, hw in enumerate(hw_items, start=1):
                        message_text += f"{idx}. 📌 <b>{hw['subject']}</b>: {hw['task_text']}\n"

                # Рассылка всем студентам и привязанным темам
                for cid, mtid in subscribers:
                    success = await self._safe_send_message(cid, message_text, message_thread_id=mtid)
                    if success:
                        total_sent += 1
                    await asyncio.sleep(0.04)

            except Exception as e:
                logger.error("Ошибка вечерней рассылки для группы %s: %s", group_id, e, exc_info=True)

        logger.info("Вечерняя рассылка (%s) завершена. Доставлено сообщений: %d", time_slot, total_sent)

    # -------------------------------------------------------------------------
    # ЗАДАЧА 1.1: ЕЖЕДНЕВНАЯ УТРЕННЯЯ РАССЫЛКА (НА СЕГОДНЯ) ПО СЛОТАМ
    # -------------------------------------------------------------------------

    async def broadcast_today_morning_schedule(self, time_slot: str = "08:00") -> None:
        """
        Ежедневная утренняя рассылка расписания на сегодня для выбранного временного слота (по умолчанию 08:00).
        Группирует пользователей и темы по group_id, делая 1 парсинг на группу.
        Также включает домашнее задание на текущий день при наличии.
        """
        logger.info("Запуск утренней рассылки расписания на сегодня (слот %s)...", time_slot)
        subscribers_map = await self.db.get_subscribers_for_morning_slot(time_slot)

        if not subscribers_map:
            logger.info("Нет активных пользователей или чатов для утреннего слота %s.", time_slot)
            return

        today = self.get_current_date()
        total_sent = 0
        for group_id, subscribers in subscribers_map.items():
            try:
                group = await self.db.get_group_by_id(group_id)
                group_name = group["group_name"] if group else group_id
                group_url = group["relative_url"] if group else ""

                sched = await timetable_parser.fetch_day_schedule(
                    group_id=group_id,
                    group_url=group_url,
                    target_date=today,
                )

                header = f"🌅 <b>Доброе утро! Расписание на сегодня ({time_slot}):</b>\n\n"
                message_text = header + format_day_schedule_message(sched, group_name)

                # Добавляем домашнее задание на сегодня при наличии
                today_fmt = today.strftime("%d.%m.%Y")
                hw_items = await self.db.get_homework_for_date(group_id, today_fmt)
                if not hw_items:
                    hw_items = await self.db.get_homework_for_date(group_id, today.isoformat())
                if hw_items:
                    message_text += "\n\n📚 <b>Домашнее задание на сегодня:</b>\n"
                    for idx, hw in enumerate(hw_items, start=1):
                        message_text += f"{idx}. 📌 <b>{hw['subject']}</b>: {hw['task_text']}\n"

                for cid, mtid in subscribers:
                    success = await self._safe_send_message(cid, message_text, message_thread_id=mtid)
                    if success:
                        total_sent += 1
                    await asyncio.sleep(0.04)
            except Exception as e:
                logger.error("Ошибка утренней рассылки для группы %s: %s", group_id, e, exc_info=True)

        logger.info("Утренняя рассылка (%s) завершена. Доставлено сообщений: %d", time_slot, total_sent)

    # -------------------------------------------------------------------------
    # ЗАДАЧА 2: В 07:00 УТРЕННЕЕ ПЛАНИРОВАНИЕ ТОЧЕЧНЫХ НАПОМИНАНИЙ О ПАРАХ
    # -------------------------------------------------------------------------

    async def schedule_today_pairs_notifications(self) -> None:
        """
        В 07:00 (и при старте бота): Анализирует расписание на сегодня для всех групп
        и создает точечные задачи APScheduler в точное время начала каждой пары
        (а также предварительные напоминания за 5, 10, 15, 20, 30, 45 минут).
        """
        today = self.get_current_date()
        now = datetime.now(self.tz)
        logger.info("Запуск планирования уведомлений о начале пар (дата: %s)...", today)

        subscribers_map = await self._get_all_subscribers_by_group()
        if not subscribers_map:
            logger.info("Нет активных подписчиков для напоминаний о парах.")
            return

        scheduled_jobs_count = 0

        for group_id, subscribers in subscribers_map.items():
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
                        pair_num = lesson.get("pair_number", 1)
                        sub_num = lesson.get("subgroup", 0)

                        # Стандартное напоминание в момент звонка (0 минут)
                        job_id = f"pair_{group_id}_{today}_{pair_num}_{start_time_str}_{sub_num}"
                        self.scheduler.add_job(
                            self._send_pair_start_alert,
                            trigger=DateTrigger(run_date=pair_datetime, timezone=self.tz),
                            id=job_id,
                            replace_existing=True,
                            kwargs={
                                "group_id": group_id,
                                "group_name": group_name,
                                "lesson": lesson,
                                "lead_minutes": 0,
                            }
                        )
                        scheduled_jobs_count += 1

                        # Предварительные напоминания (5, 10, 15, 20, 30, 45 минут)
                        for lead_m in (5, 10, 15, 20, 30, 45):
                            lead_trigger_dt = pair_datetime - timedelta(minutes=lead_m)
                            if lead_trigger_dt > now:
                                lead_job_id = f"pair_{group_id}_{today}_{pair_num}_{start_time_str}_{sub_num}_lead_{lead_m}"
                                self.scheduler.add_job(
                                    self._send_pair_start_alert,
                                    trigger=DateTrigger(run_date=lead_trigger_dt, timezone=self.tz),
                                    id=lead_job_id,
                                    replace_existing=True,
                                    kwargs={
                                        "group_id": group_id,
                                        "group_name": group_name,
                                        "lesson": lesson,
                                        "lead_minutes": lead_m,
                                    }
                                )
                                scheduled_jobs_count += 1

            except Exception as e:
                logger.error("Ошибка планирования пар для группы %s: %s", group_id, e, exc_info=True)

        logger.info("Планирование пар завершено. Запланировано точечных напоминаний: %d", scheduled_jobs_count)

    async def _send_pair_start_alert(
        self, group_id: str, group_name: str, lesson: dict[str, Any], lead_minutes: int = 0
    ) -> None:
        """
        Точечный триггер: отправляет уведомление о начале пары (или за N минут до нее).
        """
        logger.info(
            "Отправка напоминания о паре для группы %s (%s, lead=%d мин)",
            group_name, lesson.get("subject"), lead_minutes
        )

        lead_map = await self._get_subscribers_with_lead_by_group()
        group_leads = lead_map.get(group_id, {})

        if lead_minutes == 0:
            target_subscribers = group_leads.get(0, [])
            if not target_subscribers and not group_leads:
                all_subs = await self._get_all_subscribers_by_group()
                target_subscribers = all_subs.get(group_id, [])
            text = format_pair_notification(lesson, group_name)
        else:
            target_subscribers = group_leads.get(lead_minutes, [])
            base_notif = format_pair_notification(lesson, group_name)
            text = f"⏱ <b>Через {lead_minutes} минут начнётся пара!</b>\n\n" + base_notif

        if not target_subscribers:
            return

        for cid, mtid in target_subscribers:
            await self._safe_send_message(cid, text, message_thread_id=mtid)
            await asyncio.sleep(0.04)

    # -------------------------------------------------------------------------
    # ЗАДАЧА 3: МОНИТОРИНГ ЗАМЕН И ИЗМЕНЕНИЙ КАЖДЫЕ 15 МИНУТ
    # -------------------------------------------------------------------------

    async def check_timetable_substitutions_and_changes(self) -> None:
        """
        Каждые 15 минут: проверка изменений расписания на текущий/следующий день
        через парсинг и сравнение хэшей (hash).
        Если расписание изменилось, отправляет предупреждение в ЛС и темы:
        «⚠️ Внимание! Изменение в расписании!».
        """
        logger.info("Запуск 15-минутного мониторинга изменений расписания и замен...")
        subscribers_map = await self._get_all_subscribers_by_group()
        if not subscribers_map:
            return

        today = self.get_current_date()
        dates_to_check = [today, today + timedelta(days=1)]

        for group_id, subscribers in subscribers_map.items():
            group = await self.db.get_group_by_id(group_id)
            group_name = group["group_name"] if group else group_id
            group_url = group["relative_url"] if group else ""

            for target_date in dates_to_check:
                date_str = target_date.isoformat()
                try:
                    # 1. Считываем сохраненный кэш и хэш
                    cached_entry = await self.db.get_cached_timetable_entry(group_id, date_str)
                    old_hash = cached_entry.get("hash") if cached_entry else None

                    # 2. Получаем свежее расписание напрямую с сайта/API
                    fresh_sched = await timetable_parser.fetch_day_schedule(
                        group_id=group_id,
                        group_url=group_url,
                        target_date=target_date,
                        force_refresh=True,
                    )
                    new_hash = compute_schedule_hash(fresh_sched)

                    # 3. Если старый хэш существовал и отличается от нового -> изменение / замена!
                    if old_hash and old_hash != new_hash:
                        logger.warning(
                            "ОБНАРУЖЕНО ИЗМЕНЕНИЕ В РАСПИСАНИИ для группы %s на %s! (старый: %s, новый: %s)",
                            group_name, date_str, old_hash[:8], new_hash[:8]
                        )
                        header = (
                            "⚠️ <b>Внимание! Изменение в расписании!</b>\n"
                            f"Обнаружена замена или корректировка занятий для группы <code>{group_name}</code>:\n\n"
                        )
                        alert_text = header + format_day_schedule_message(fresh_sched, group_name)

                        # Рассылаем во все подписанные ЛС и темы
                        for cid, mtid in subscribers:
                            await self._safe_send_message(cid, alert_text, message_thread_id=mtid)
                            await asyncio.sleep(0.04)

                    # Обновляем кэш с новым хэшем
                    await self.db.save_cached_timetable(group_id, date_str, fresh_sched, data_hash=new_hash)

                except Exception as e:
                    logger.error("Ошибка мониторинга расписания для группы %s (%s): %s", group_id, date_str, e)

    async def check_user_note_reminders(self) -> None:
        """
        Проверяет заметки студентов и отправляет напоминания в день дедлайна перед нужной парой.
        Запускается каждые 10 минут.
        """
        now_dt = datetime.now(self.tz)
        today_str = now_dt.strftime("%Y-%m-%d")
        cur_min = now_dt.hour * 60 + now_dt.minute

        pending_notes = await self.db.get_pending_note_reminders(today_str)
        if not pending_notes:
            return

        pair_starts = {
            1: (8 * 60 + 30, "08:30"),
            2: (10 * 60 + 10, "10:10"),
            3: (11 * 60 + 50, "11:50"),
            4: (14 * 60 + 0, "14:00"),
            5: (15 * 60 + 40, "15:40"),
            6: (17 * 60 + 20, "17:20"),
            7: (19 * 60 + 0, "19:00"),
        }

        for nt in pending_notes:
            note_id = nt["id"]
            user_id = nt["user_id"]
            note_target_date = nt.get("target_date")
            target_pair = nt.get("target_pair")
            text = nt.get("note_text", "")

            # Если у заметки есть дата и это не сегодня — пропускаем
            # (могут прийти просроченные заметки)
            if note_target_date and note_target_date != today_str:
                continue

            should_remind = False
            time_desc = ""

            if target_pair is not None and target_pair > 0:
                start_min, start_str = pair_starts.get(target_pair, (8 * 60 + 30, "08:30"))
                # Напоминаем за 45 минут до пары или если время уже подошло
                if cur_min >= start_min - 45:
                    should_remind = True
                    time_desc = f"к {target_pair}-й паре ({start_str})"
            elif target_pair == 0:
                # К началу дня
                if cur_min >= 8 * 60:
                    should_remind = True
                    time_desc = "к началу учебного дня"
            else:
                # Без пары: напоминаем утром
                if cur_min >= 8 * 60 + 30:
                    should_remind = True
                    time_desc = "сегодня"

            if should_remind:
                msg = (
                    f"⏰ <b>Напоминание по вашей личной заметке!</b>\n\n"
                    f"📌 <b>{text}</b>\n"
                    f"🗓 Срок выполнения: <b>{time_desc}</b>\n\n"
                    "<i>Не забудьте подготовиться и сдать работу вовремя!</i>"
                )
                sent = await self._safe_send_message(chat_id=user_id, text=msg)
                if sent:
                    await self.db.mark_note_reminded(note_id)

    async def cleanup_old_notes(self) -> None:
        """
        Ежедневная автоочистка напомненных заметок старше 7 дней.
        Запускается один раз в сутки в 04:00.
        """
        try:
            deleted = await self.db.cleanup_old_reminded_notes(days_old=7)
            logger.info("Автоочистка заметок: удалено %d записей.", deleted)
        except Exception as e:
            logger.error("Ошибка при автоочистке заметок: %s", e)

    async def prewarm_morning_cache(self) -> None:
        """
        Тихий утренний прогрев кэша в 07:55 (за 5 минут до утренней рассылки в 08:00).
        Загружает расписание на сегодня для всех активных групп колледжа в RAM и SQLite,
        чтобы в 08:00 рассылка вылетала мгновенно без обращений к сайту КТМУ.
        """
        logger.info("Запуск тихого утреннего прогрева кэша в 07:55...")
        try:
            today = self.get_current_date()
            subscribers = await self._get_all_subscribers_by_group()
            active_group_ids = list(subscribers.keys())
            if not active_group_ids:
                logger.info("Тихий прогрев 07:55: активных групп нет.")
                return

            logger.info("Тихий прогрев 07:55 для %d активных групп...", len(active_group_ids))
            for gid in active_group_ids:
                try:
                    await timetable_parser.fetch_day_schedule(
                        group_id=gid,
                        target_date=today,
                        force_refresh=True
                    )
                except Exception as e:
                    logger.debug("Ошибка прогрева группы %s: %s", gid, e)
                await asyncio.sleep(0.05)

            logger.info("Тихий утренний прогрев кэша в 07:55 успешно завершен!")
        except Exception as e:
            logger.error("Ошибка при тихом прогреве кэша в 07:55: %s", e)

    async def send_weekly_db_backup(self) -> None:
        """
        Еженедельный автоматический бэкап базы данных SQLite в ЛС администратору.
        Запускается каждое воскресенье в 04:30 утра.
        """
        logger.info("Запуск еженедельного автобэкапа базы данных...")
        backup_path = None
        try:
            admin_ids = config.ADMIN_IDS
            if not admin_ids:
                logger.warning("Автобэкап БД: список ADMIN_IDS пуст.")
                return

            main_admin_id = admin_ids[0]
            backup_path = await self.db.create_backup_file()

            from aiogram.types import FSInputFile
            now_str = datetime.now().strftime("%d.%m.%Y %H:%M")
            stats = await self.db.get_admin_stats()
            caption = (
                f"📦 <b>Автоматический еженедельный бэкап базы данных КТМУ</b>\n\n"
                f"📅 <b>Дата:</b> {now_str}\n"
                f"👥 <b>Пользователей в базе:</b> {stats.get('total_users', 0)}\n"
                f"💬 <b>Привязанных чатов:</b> {stats.get('total_chats', 0)}\n"
                f"👑 <b>Старост:</b> {stats.get('total_starostas', 0)}\n"
                f"📝 <b>Записей Д/З:</b> {stats.get('total_hw', 0)}\n\n"
                f"<i>Файл содержит полный консистентный снимок SQLite (включая WAL журнал).</i>"
            )
            doc = FSInputFile(backup_path, filename=os.path.basename(backup_path))
            await self.bot.send_document(
                chat_id=main_admin_id,
                document=doc,
                caption=caption,
                parse_mode="HTML"
            )
            logger.info("Еженедельный автобэкап БД успешно отправлен администратору %d!", main_admin_id)
        except Exception as e:
            logger.error("Ошибка при отправке еженедельного автобэкапа БД: %s", e)
        finally:
            if backup_path and os.path.exists(backup_path):
                try:
                    os.remove(backup_path)
                except Exception:
                    pass

    # -------------------------------------------------------------------------
    # ИНИЦИАЛИЗАЦИЯ И СТАРТ
    # -------------------------------------------------------------------------

    def start(self) -> None:
        """
        Запускает крон-задачи планировщика в соответствии с ТЗ.
        """
        # 1. Вечерняя рассылка расписания на завтра по временным слотам (18:00, 19:00, 20:00, 21:00, 22:00)
        evening_slots = [(18, 0), (19, 0), (20, 0), (21, 0), (22, 0)]
        for h, m in evening_slots:
            slot_str = f"{h:02d}:{m:02d}"
            self.scheduler.add_job(
                self.broadcast_tomorrow_schedule,
                trigger=CronTrigger(hour=h, minute=m, timezone=self.tz),
                args=[slot_str],
                id=f"daily_evening_broadcast_{slot_str.replace(':', '_')}",
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

        # 3. Утренняя рассылка расписания на сегодня по временным слотам (07:00, 07:30, 08:00, 08:30, 09:00)
        morning_slots = [(7, 0), (7, 30), (8, 0), (8, 30), (9, 0)]
        for h, m in morning_slots:
            slot_str = f"{h:02d}:{m:02d}"
            self.scheduler.add_job(
                self.broadcast_today_morning_schedule,
                trigger=CronTrigger(hour=h, minute=m, timezone=self.tz),
                args=[slot_str],
                id=f"daily_morning_broadcast_{slot_str.replace(':', '_')}",
                replace_existing=True,
                misfire_grace_time=300
            )

        # 4. Мониторинг замен и изменений расписания каждые 15 минут
        self.scheduler.add_job(
            self.check_timetable_substitutions_and_changes,
            trigger=CronTrigger(minute="*/15", timezone=self.tz),
            id="changes_monitoring_15_minutes",
            replace_existing=True,
            misfire_grace_time=300
        )

        # 5. Напоминания по личным заметкам каждые 10 минут
        self.scheduler.add_job(
            self.check_user_note_reminders,
            trigger=CronTrigger(minute="*/10", timezone=self.tz),
            id="notes_reminders_10_minutes",
            replace_existing=True,
            misfire_grace_time=300,
        )

        # 6. Ежедневная автоочистка напомненных заметок старше 7 дней в 04:00
        self.scheduler.add_job(
            self.cleanup_old_notes,
            trigger=CronTrigger(hour=4, minute=0, timezone=self.tz),
            id="daily_notes_cleanup_04_00",
            replace_existing=True,
            misfire_grace_time=600,
        )

        # 7. Тихий утренний прогрев кэша в 07:55 перед утренней рассылкой в 08:00
        self.scheduler.add_job(
            self.prewarm_morning_cache,
            trigger=CronTrigger(hour=7, minute=55, timezone=self.tz),
            id="prewarm_morning_cache_07_55",
            replace_existing=True,
            misfire_grace_time=300,
        )

        # 8. Еженедельный автоматический бэкап базы данных в ЛС админу по воскресеньям в 04:30
        self.scheduler.add_job(
            self.send_weekly_db_backup,
            trigger=CronTrigger(day_of_week="sun", hour=4, minute=30, timezone=self.tz),
            id="weekly_db_backup_sunday_04_30",
            replace_existing=True,
            misfire_grace_time=3600,
        )

        self.scheduler.start()
        logger.info(
            "APScheduler успешно запущен: крон-задачи на слоты рассылок, напоминания и мониторинг каждые 15 мин (%s)",
            config.TIMEZONE
        )

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
