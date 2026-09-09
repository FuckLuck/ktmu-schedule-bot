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
    DATABASE_PATH: str = "ktmu_bot.db"
    TIMEZONE: str = "Europe/Moscow"
    CACHE_TTL_HOURS: int = 6
    THROTTLING_RATE_LIMIT: float = 0.8

    # Время стандартных пар КТМУ по умолчанию (если не пришли из API)
    DEFAULT_PERIOD_TIMES: list[str] = [
        "08:30-10:00",
        "10:10-11:40",
        "11:50-13:20",
        "14:00-15:30",
        "15:40-17:10",
        "17:20-18:50",
    ]


config = Settings()
