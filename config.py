import os
from pathlib import Path
from pydantic_settings import BaseSettings, SettingsConfigDict

BASE_DIR = Path(__file__).resolve().parent


class Settings(BaseSettings):
    """
    Конфигурация Telegram-бота расписания КТМУ.
    Считывает переменные окружения из .env файла и системного окружения.
    """
    model_config = SettingsConfigDict(
        env_file=os.path.join(BASE_DIR, ".env"),
        env_file_encoding="utf-8",
        extra="ignore"
    )

    BOT_TOKEN: str = "YOUR_TELEGRAM_BOT_TOKEN_HERE"
    BASE_URL: str = "https://timetable-ktmu.ru"
    DATABASE_PATH: str = os.getenv("DATABASE_PATH", "ktmu_bot.db")
    TIMEZONE: str = "Europe/Moscow"
    CACHE_TTL_HOURS: int = 6
    THROTTLING_RATE_LIMIT: float = 0.8
    ADMIN_IDS_RAW: str = "870396858"
    WEBAPP_CAMPUS_URL: str = os.getenv("WEBAPP_CAMPUS_URL", "")
    WEBAPP_SCHEDULE_URL: str = os.getenv("WEBAPP_SCHEDULE_URL", "https://ktmu-schedule-bot-plqd-one.vercel.app/")
    WEB_SERVER_HOST: str = os.getenv("WEB_SERVER_HOST", "0.0.0.0")
    WEB_SERVER_PORT: int = int(os.getenv("WEB_SERVER_PORT", "8080"))

    @property
    def ADMIN_IDS(self) -> list[int]:
        """Парсит список ID администраторов из строки с разделителями."""
        ids: list[int] = []
        for part in str(self.ADMIN_IDS_RAW).split(","):
            part = part.strip()
            if part.isdigit():
                ids.append(int(part))
        return ids if ids else [870396858]

    def is_admin(self, user_id: int) -> bool:
        """Проверяет, является ли пользователь администратором."""
        return user_id in self.ADMIN_IDS

    # Время стандартных пар КТМУ по умолчанию (если не пришли из API)
    DEFAULT_PERIOD_TIMES: list[str] = [
        "08:30-10:00",
        "10:10-11:40",
        "11:50-13:20",
        "14:00-15:30",
        "15:40-17:10",
        "17:20-18:50",
        "19:00-20:30",
    ]

    # Время пар для корпуса на Вознесенском пр., 44 по умолчанию
    DEFAULT_VOZNESENSKY_PERIOD_TIMES: list[str] = [
        "08:30-09:55",
        "10:05-11:30",
        "11:40-13:05",
        "13:45-15:10",
        "15:20-16:45",
        "16:55-18:20",
        "18:30-20:00",
    ]


config = Settings()
