"""
Встроенный HTTP-сервер для Telegram Mini App и REST API расписания.
Работает на aiohttp.web, раздает статические файлы webapp и предоставляет API.
"""

import json
import logging
from pathlib import Path
from typing import Optional
from aiohttp import web

from config import Settings
from database import Database, db as default_db
from timetable_parser import timetable_parser

logger = logging.getLogger(__name__)

WEBAPP_DIR = Path(__file__).resolve().parent / "webapp"


async def handle_health(request: web.Request) -> web.Response:
    """Проверка жизнеспособности сервиса."""
    return web.json_response({"status": "ok", "app": "ktmu-schedule-webapp"})


async def handle_get_schedule(request: web.Request) -> web.Response:
    """
    API эндпоинт: возвращает расписание на неделю для указанной группы.
    GET /api/schedule?group_id=...&group_name=...
    """
    group_id = request.query.get("group_id", "").strip()
    group_name = request.query.get("group_name", "").strip()

    if not group_id and not group_name:
        return web.json_response({"error": "group_id or group_name required"}, status=400)

    db: Database = request.app.get("database", default_db)

    # Если передан group_name, ищем group_id в БД
    if not group_id and group_name:
        groups = await db.get_all_groups()
        for g in groups:
            if g.get("group_name", "").strip().lower() == group_name.lower():
                group_id = g.get("id", "")
                break

    if not group_id:
        return web.json_response({"error": f"group '{group_name}' not found"}, status=404)

    group_data = await db.get_group_by_id(group_id)
    url = group_data.get("relative_url", "") if group_data else ""
    actual_name = group_data.get("group_name", group_name or group_id) if group_data else group_name

    try:
        week_schedule = await timetable_parser.fetch_week_schedule(
            group_id=group_id, group_url=url
        )
        return web.json_response({
            "group_id": group_id,
            "group_name": actual_name,
            "days": week_schedule
        })
    except Exception as e:
        logger.error("Ошибка API /api/schedule для %s: %s", group_id, e)
        return web.json_response(
            {"error": "failed to fetch schedule", "details": str(e)},
            status=500,
            headers={"Access-Control-Allow-Origin": "*"}
        )


async def handle_toggle_skip(request: web.Request) -> web.Response:
    """
    API эндпоинт: переключение статуса пропуска пары студентом.
    POST /api/skip
    Body: {"user_id": int, "date": "YYYY-MM-DD", "pair_number": int}
    """
    db: Database = request.app.get("database", default_db)
    try:
        data = await request.json()
    except Exception:
        return web.json_response(
            {"error": "invalid json"},
            status=400,
            headers={"Access-Control-Allow-Origin": "*"}
        )

    user_id = data.get("user_id")
    date_str = str(data.get("date", "")).strip()
    pair_number = data.get("pair_number")

    if not user_id or not date_str or pair_number is None:
        return web.json_response(
            {"error": "user_id, date, and pair_number required"},
            status=400,
            headers={"Access-Control-Allow-Origin": "*"}
        )

    try:
        user_id = int(user_id)
        pair_number = int(pair_number)
    except (ValueError, TypeError):
        return web.json_response(
            {"error": "invalid user_id or pair_number"},
            status=400,
            headers={"Access-Control-Allow-Origin": "*"}
        )

    is_skipped = await db.toggle_skipped_pair(user_id, date_str, pair_number)
    month_str = date_str[:7]
    stats = await db.get_monthly_skipped_stats(user_id, month_str)

    return web.json_response({
        "status": "ok",
        "user_id": user_id,
        "date": date_str,
        "pair_number": pair_number,
        "is_skipped": is_skipped,
        "monthly_total": stats.get("total_skipped_pairs", 0),
        "monthly_hours": stats.get("total_academic_hours", 0)
    }, headers={"Access-Control-Allow-Origin": "*"})


async def handle_get_skips(request: web.Request) -> web.Response:
    """
    API эндпоинт: получение списка и статистики пропущенных пар студента.
    GET /api/skips?user_id=123&month=YYYY-MM
    """
    db: Database = request.app.get("database", default_db)
    user_id_raw = request.query.get("user_id", "").strip()
    month = request.query.get("month", "").strip() or None

    if not user_id_raw or not user_id_raw.isdigit():
        return web.json_response(
            {"error": "valid user_id required"},
            status=400,
            headers={"Access-Control-Allow-Origin": "*"}
        )

    user_id = int(user_id_raw)
    stats = await db.get_monthly_skipped_stats(user_id, month)
    return web.json_response(stats, headers={"Access-Control-Allow-Origin": "*"})


async def handle_options(request: web.Request) -> web.Response:
    """Обработчик CORS preflight OPTIONS запросов."""
    return web.Response(
        status=200,
        headers={
            "Access-Control-Allow-Origin": "*",
            "Access-Control-Allow-Methods": "GET, POST, OPTIONS",
            "Access-Control-Allow-Headers": "Content-Type",
        },
    )


def create_web_app(database: Optional[Database] = None) -> web.Application:
    """Создает и настраивает aiohttp web-приложение."""
    app = web.Application()
    app["database"] = database or default_db

    # API маршруты
    app.router.add_get("/health", handle_health)
    app.router.add_get("/api/schedule", handle_get_schedule)
    app.router.add_post("/api/skip", handle_toggle_skip)
    app.router.add_get("/api/skips", handle_get_skips)
    app.router.add_route("OPTIONS", "/api/{tail:.*}", handle_options)

    # Раздача статики WebApp
    if WEBAPP_DIR.exists():
        app.router.add_static("/webapp", WEBAPP_DIR, show_index=True)
        # Главная страница перенаправляет на webapp
        async def handle_root(request: web.Request) -> web.Response:
            raise web.HTTPFound("/webapp/")
        app.router.add_get("/", handle_root)

    return app


async def start_web_server(
    host: str = "0.0.0.0", port: int = 8080, database: Optional[Database] = None
) -> web.AppRunner:
    """Запускает web-сервер в текущем asyncio event loop."""
    app = create_web_app(database)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, host, port)
    await site.start()
    logger.info("Web-сервер Telegram Mini App успешно запущен на http://%s:%d/webapp/", host, port)
    return runner
