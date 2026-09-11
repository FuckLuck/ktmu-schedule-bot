import logging
import time
from typing import Any, Awaitable, Callable, Dict, Optional

from aiogram import BaseMiddleware
from aiogram.types import CallbackQuery, Message, TelegramObject, User

logger = logging.getLogger("ktmu_bot.throttling")


class ThrottlingMiddleware(BaseMiddleware):
    """
    Middleware для защиты от спама и ограничения частоты запросов (Rate Limiting / Throttling).
    
    Особенности:
    1. Перехватывает как обычные текстовые сообщения и команды (Message),
       так и нажатия на inline-кнопки (CallbackQuery).
    2. При превышении лимита скорости:
       - Для Message: отправляет однократное вежливое предупреждение и заглушает
         последующий спам до истечения кулдауна (защита от циклического спама ответами).
       - Для CallbackQuery: вызывает event.answer() с уведомлением (чтобы не висел спиннер Telegram),
         не запуская основной хэндлер.
    3. Автоматическая самоочистка кэша от устаревших записей для экономии памяти.
    """

    def __init__(self, rate_limit: float = 0.8, cleanup_threshold: int = 5000):
        """
        :param rate_limit: Минимальный интервал между запросами от одного пользователя (в секундах).
        :param cleanup_threshold: Порог записей, при превышении которого запускается очистка старых временных меток.
        """
        super().__init__()
        self.rate_limit = rate_limit
        self.cleanup_threshold = cleanup_threshold
        # Хранилище: user_id -> time.monotonic() последнего успешного запроса
        self._last_request_time: Dict[int, float] = {}
        # Хранилище: user_id -> bool (было ли отправлено предупреждение о спаме в текущей серии)
        self._user_warned: Dict[int, bool] = {}

    async def __call__(
        self,
        handler: Callable[[TelegramObject, Dict[str, Any]], Awaitable[Any]],
        event: TelegramObject,
        data: Dict[str, Any],
    ) -> Any:
        # Если троттлинг отключен (лимит <= 0), пропускаем без проверок
        if self.rate_limit <= 0:
            return await handler(event, data)

        # Извлекаем пользователя из данных или события
        user: Optional[User] = data.get("event_from_user") or getattr(event, "from_user", None)
        if not user:
            return await handler(event, data)

        user_id = user.id
        now = time.monotonic()
        last_time = self._last_request_time.get(user_id, 0.0)
        elapsed = now - last_time

        # Проверка на превышение частоты запросов
        if elapsed < self.rate_limit:
            logger.debug(
                "Троттлинг пользователя ID %d: прошло %.2fс, лимит %.2fс",
                user_id,
                elapsed,
                self.rate_limit,
            )

            # Обработка нажатий на инлайн-кнопки
            if isinstance(event, CallbackQuery):
                try:
                    await event.answer("⏳ Слишком частые нажатия! Подождите секунду.", show_alert=False)
                except Exception as e:
                    logger.debug("Не удалось отправить alert на callback_query: %s", e)
                return None

            # Обработка входящих сообщений
            if isinstance(event, Message):
                # Отправляем предупреждение только 1 раз за спам-серию, чтобы бот сам не спамил
                if not self._user_warned.get(user_id, False):
                    self._user_warned[user_id] = True
                    try:
                        await event.answer(
                            "⏳ <b>Пожалуйста, не спамьте!</b>\n"
                            "Подождите секунду перед следующим действием."
                        )
                    except Exception as e:
                        logger.debug("Не удалось отправить сообщение о троттлинге: %s", e)
                return None

            return None

        # Запрос прошел проверку по времени
        self._last_request_time[user_id] = now
        self._user_warned[user_id] = False

        # Фоновая периодическая очистка кэша, если пользователей накопилось много
        if len(self._last_request_time) > self.cleanup_threshold:
            self._cleanup_stale_records(now)

        return await handler(event, data)

    def _cleanup_stale_records(self, now: float) -> None:
        """Удаляет записи пользователей, не проявлявших активность более 10 минут."""
        cutoff = now - 600.0  # 10 минут
        stale_users = [uid for uid, t in self._last_request_time.items() if t < cutoff]
        for uid in stale_users:
            self._last_request_time.pop(uid, None)
            self._user_warned.pop(uid, None)
        logger.info("Очистка троттлинг-кэша: удалено %d устаревших записей", len(stale_users))


NAV_MENU_BUTTONS: set[str] = {
    # Главное меню
    "📅 На сегодня", "📅 Today",
    "📆 На завтра", "🌅 Tomorrow",
    "🗓 На неделю", "📆 Full Week",
    "⏰ Звонки", "⏰ Bells",
    "🎓 Меню группы (ДЗ / Староста)", "🎓 Group menu (HW / Starosta)", "🎓 Меню группы",
    "🔍 Поиск преподавателя", "🔍 Teacher search",
    "⚙️ Настройки и связь", "⚙️ Settings & info", "⚙️ Настройки",
    "👑 Админ-панель", "👑 Admin Panel",

    # Меню группы
    "📚 ДЗ 📚", "📚 Homework 📚", "📚 ДЗ",
    "💬 Чат группы", "💬 Group chat",
    "📝 Личные заметки", "📝 Personal notes",
    "🙋‍♂️ Староста 🙋‍♂️", "🙋‍♂️ Starosta 🙋‍♂️",
    "⚙️ Сменить группу", "⚙️ Change group",

    # Настройки и связь
    "🌐 Язык", "🌐 Language",
    "ℹ️ Бот в группу", "➕ Bot to group",
    "👨‍💻 Связь с автором", "👨‍💻 Contact author",
    "📖 Инструкция", "📖 Guide",

    # Возврат в меню
    "⬅️ Главное меню", "⬅️ Main menu", "🏠 Главное меню", "В главное меню",
}


def is_nav_button_or_command(text: Optional[str]) -> bool:
    """Проверяет, является ли введенный текст кнопкой навигации или командой бота."""
    if not text:
        return False
    clean = text.strip()
    if clean.startswith("/"):
        return True
    if "Уведомления" in clean or "Notifs" in clean:
        return True
    return clean in NAV_MENU_BUTTONS


class MenuNavResetMiddleware(BaseMiddleware):
    """
    Middleware, гарантирующий сброс FSM-состояния при нажатии на кнопки меню или команды.
    Предотвращает ошибочный перехват меню состояниями ввода (поиск, заметки, ДЗ и т.д.).
    """

    async def __call__(
        self,
        handler: Callable[[TelegramObject, Dict[str, Any]], Awaitable[Any]],
        event: TelegramObject,
        data: Dict[str, Any],
    ) -> Any:
        state = data.get("state")
        if state:
            if isinstance(event, Message) and event.text:
                if is_nav_button_or_command(event.text):
                    cur_state = await state.get_state()
                    if cur_state is not None:
                        await state.clear()
            elif isinstance(event, CallbackQuery) and event.data:
                if event.data.startswith("nav:main_menu"):
                    cur_state = await state.get_state()
                    if cur_state is not None:
                        await state.clear()

        return await handler(event, data)

