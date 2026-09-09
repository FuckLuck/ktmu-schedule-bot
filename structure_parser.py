import asyncio
import json
import logging
import re
from typing import Any
from urllib.parse import urljoin

import aiohttp
from bs4 import BeautifulSoup

from config import config
from database import Database, db as default_db

logger = logging.getLogger(__name__)


# Общероссийский классификатор специальностей СПО для КТМУ
STANDARD_SPECIALTIES = {
    "09.02.11": "09.02.11 Информационные системы и программирование",
    "09.02.13": "09.02.13 Компьютерные системы и комплексы",
    "29.02.10": "29.02.10 Моделирование и технология швейных изделий",
    "38.02.02": "38.02.02 Страховое дело",
    "38.02.08": "38.02.08 Торговое дело",
    "42.02.01": "42.02.01 Реклама",
    "54.02.01": "54.02.01 Дизайн",
}


class StructureParser:
    """
    Парсер структуры сайта расписания колледжа (https://timetable-ktmu.ru/).
    Реализует гибридный подход:
    1. Парсинг HTML-ссылок через BeautifulSoup4 (в соответствии с классической моделью сайта).
    2. Прямой разбор API хранилища данных (на случай SPA-рендеринга на стороне клиента).
    """

    def __init__(self, base_url: str = config.BASE_URL, database: Database = default_db):
        self.base_url = base_url.rstrip("/")
        self.db = database
        self.headers = {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                          "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
            "Accept": "text/html,application/json,application/xhtml+xml,*/*",
            "Accept-Language": "ru-RU,ru;q=0.9,en-US;q=0.8,en;q=0.7",
        }

    async def _fetch_text(self, session: aiohttp.ClientSession, url: str) -> str:
        for attempt in range(3):
            try:
                async with session.get(url, headers=self.headers, timeout=aiohttp.ClientTimeout(total=15)) as resp:
                    if resp.status == 503:
                        await asyncio.sleep(1.5 * (attempt + 1))
                        continue
                    resp.raise_for_status()
                    return await resp.text()
            except Exception as e:
                if attempt == 2:
                    raise
                await asyncio.sleep(1.5 * (attempt + 1))
        return ""

    async def _fetch_json(self, session: aiohttp.ClientSession, url: str) -> dict[str, Any]:
        for attempt in range(3):
            try:
                async with session.get(url, headers=self.headers, timeout=aiohttp.ClientTimeout(total=20)) as resp:
                    if resp.status == 503:
                        await asyncio.sleep(1.5 * (attempt + 1))
                        continue
                    resp.raise_for_status()
                    data = await resp.json(content_type=None)
                    if isinstance(data, str):
                        return json.loads(data)
                    return data
            except Exception as e:
                if attempt == 2:
                    raise
                await asyncio.sleep(1.5 * (attempt + 1))
        return {}

    async def _parse_html_structure(self, session: aiohttp.ClientSession) -> list[dict[str, str]]:
        """
        Классический парсинг HTML с обходом по ссылкам через BeautifulSoup4.
        1. Заходит на главную страницу.
        2. Ищет ссылки на специальности (href содержит /specialty/).
        3. Заходит на каждую специальность и парсит группы (/specialty/.../year/.../group/...).
        """
        groups: list[dict[str, str]] = []
        try:
            main_html = await self._fetch_text(session, self.base_url)
            if not main_html:
                return []
            soup = BeautifulSoup(main_html, "html.parser")

            # 1. Поиск ссылок на специальности
            specialty_links = soup.find_all("a", href=re.compile(r"/specialty/[^/]+"))
            logger.info("Парсинг HTML: найдено %d потенциальных ссылок на специальности", len(specialty_links))

            visited_specialties = set()
            for spec_tag in specialty_links:
                href = spec_tag.get("href", "")
                spec_name = spec_tag.get_text(strip=True) or "Специальность"
                spec_url = urljoin(self.base_url, href)

                if spec_url in visited_specialties:
                    continue
                visited_specialties.add(spec_url)

                try:
                    spec_html = await self._fetch_text(session, spec_url)
                    spec_soup = BeautifulSoup(spec_html, "html.parser")

                    # 2. Поиск групп на странице специальности
                    group_tags = spec_soup.find_all("a", href=re.compile(r"/specialty/.+/year/.+/group/[^/]+"))
                    for grp_tag in group_tags:
                        grp_href = grp_tag.get("href", "")
                        grp_name = grp_tag.get_text(strip=True)
                        if not grp_name:
                            continue

                        # Извлечение group_id из ссылки
                        match = re.search(r"/group/([^/?#]+)", grp_href)
                        group_id = match.group(1) if match else grp_name

                        groups.append({
                            "id": group_id,
                            "specialty_name": spec_name,
                            "group_name": grp_name,
                            "relative_url": grp_href if grp_href.startswith("/") else f"/{grp_href}"
                        })
                except Exception as e:
                    logger.debug("Не удалось распарсить страницу специальности %s: %s", spec_url, e)

        except Exception as e:
            logger.debug("HTML парсинг структуры завершился с замечанием: %s", e)

        return groups

    async def _parse_storage_api_structure(self, session: aiohttp.ClientSession) -> list[dict[str, str]]:
        """
        Парсинг актуальных данных структуры из API бэкенда (timetable-ktmu.ru SPA).
        Извлекает все специальности, сопоставляет группы и формирует канонические относительные URL.
        """
        api_url = f"{self.base_url}/api/storage/schedule-data-v2"
        logger.info("Запрос структуры через API хранилища: %s", api_url)
        raw = await self._fetch_json(session, api_url)

        val = raw.get("value", {})
        if isinstance(val, str):
            val = json.loads(val)

        specialties_list = val.get("specialties", [])
        groups_list = val.get("groups", [])

        # Карта специальностей: id -> красивое название
        spec_map: dict[str, str] = {}
        for s in specialties_list:
            if "id" not in s:
                continue
            raw_name = s.get("name", "").strip()
            # Пытаемся найти известный код специальности (например, 09.02.11)
            code_match = re.search(r"\b(\d{2}\.\d{2}\.\d{2})\b", raw_name)
            if code_match and code_match.group(1) in STANDARD_SPECIALTIES:
                spec_map[s["id"]] = STANDARD_SPECIALTIES[code_match.group(1)]
            else:
                spec_map[s["id"]] = raw_name or "Специальность"

        result: list[dict[str, str]] = []
        for g in groups_list:
            gid = str(g.get("id", "")).strip()
            gname = str(g.get("name", "")).strip()
            spec_id = str(g.get("specialtyId", "")).strip()
            year = g.get("admissionYear", "")

            if not gid or not gname:
                continue

            specialty_name = spec_map.get(spec_id, "Общая специальность")
            # Формирование пути по регламенту ТЗ: /specialty/.../year/.../group/...
            year_part = f"/year/{year}" if year else ""
            relative_url = f"/specialty/{spec_id or 'all'}{year_part}/group/{gid}"

            result.append({
                "id": gid,
                "specialty_name": specialty_name,
                "group_name": gname,
                "relative_url": relative_url
            })

        logger.info("API хранилища отдало: специальностей %d, групп %d", len(spec_map), len(result))
        return result

    async def sync_structure(self) -> list[dict[str, str]]:
        """
        Главная функция синхронизации структуры колледжа:
        1. Заходит на сайт https://timetable-ktmu.ru/
        2. Обходит специальности и парсит группы и ссылки на них
        3. Сохраняет и обновляет список групп в SQLite таблице groups
        """
        logger.info("Запуск синхронизации структуры колледжа с %s ...", self.base_url)
        groups: list[dict[str, str]] = []

        async with aiohttp.ClientSession() as session:
            # Сначала проверяем HTML-ссылки по регламенту ТЗ
            try:
                groups = await self._parse_html_structure(session)
            except Exception as e:
                logger.warning("Ошибка при HTML-обходе структуры: %s", e)

            # Если HTML не вернул групп (так как сайт является клиентским React SPA),
            # используем прямой API-эндпоинт бэкенда
            if not groups:
                logger.info("HTML не содержит статических ссылок групп (SPA-клиент). Переход на API хранилища...")
                try:
                    groups = await self._parse_storage_api_structure(session)
                except Exception as e:
                    logger.error("Ошибка при запросе API хранилища структуры: %s", e, exc_info=True)

        if not groups:
            logger.error("Синхронизация структуры не принесла результатов! Проверьте доступность сайта.")
            return []

        # Сохраняем полученные группы в базу данных
        await self.db.upsert_groups_bulk(groups)
        logger.info("Синхронизация успешно завершена! Всего групп в базе: %d", len(groups))
        return groups


async def sync_structure(db: Database = default_db) -> list[dict[str, str]]:
    """
    Фасадная функция для запуска синхронизации структуры.
    """
    parser = StructureParser(base_url=config.BASE_URL, database=db)
    return await parser.sync_structure()


if __name__ == "__main__":
    # Тестовый запуск модуля синхронизации структуры
    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(name)s: %(message)s")

    async def main_test():
        await default_db.init_db()
        res = await sync_structure()
        print(f"Синхронизировано групп: {len(res)}")
        specs = await default_db.get_all_specialties()
        print(f"Специальностей: {len(specs)}")
        for sp in specs[:3]:
            grps = await default_db.get_groups_by_specialty(sp)
            print(f"  - {sp}: {[g['group_name'] for g in grps[:4]]}")

    asyncio.run(main_test())
