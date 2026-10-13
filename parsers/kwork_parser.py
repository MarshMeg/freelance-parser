import html as html_lib
import json
import logging
from datetime import datetime

from config import config
from models import Job
from utils import MSK
from .base_parser import BaseParser

logger = logging.getLogger(__name__)

BASE_URL = "https://kwork.ru"
PROJECTS_URL = f"{BASE_URL}/projects"
_STATE_KEY = '"wantsListData":'


class KworkParser(BaseParser):
    """Парсер биржи проектов kwork.ru (данные лежат JSON-ом в HTML страницы)"""

    source = "kwork"

    def __init__(self, timeout: int = 30, category_id: int | None = None, pages: int = 2):
        super().__init__(timeout)
        self.category_id = config.KWORK_CATEGORY_ID if category_id is None else category_id
        self.pages = pages

    async def parse(self) -> list[Job]:
        jobs: list[Job] = []
        for page in range(1, self.pages + 1):
            params = {"page": page}
            if self.category_id:
                params["c"] = self.category_id
            state = self._extract_state(await self.fetch_page(PROJECTS_URL, params))
            jobs.extend(self._parse_wants(state))
            if page >= state.get("last_page", page):
                break
        return jobs

    def parse_html(self, html: str) -> list[Job]:
        return self._parse_wants(self._extract_state(html))

    @staticmethod
    def _extract_state(html: str) -> dict:
        """Объект pagination из JSON-состояния страницы (`data` — заказы, `last_page`)"""
        start = html.find(_STATE_KEY)
        if start == -1:
            logger.error("На странице kwork нет %s", _STATE_KEY)
            return {}
        try:
            data, _ = json.JSONDecoder().raw_decode(html, start + len(_STATE_KEY))
            return data["pagination"]
        except (ValueError, KeyError) as e:
            logger.error("Не удалось разобрать состояние kwork: %s", e)
            return {}

    def _parse_wants(self, state: dict) -> list[Job]:
        jobs: list[Job] = []
        for want in state.get("data", []):
            try:
                jobs.append(self._parse_want(want))
            except Exception as e:
                logger.warning("Не удалось разобрать проект kwork %s: %s", want.get("id"), e)
        return jobs

    def _parse_want(self, want: dict) -> Job:
        budget = float(want["priceLimit"]) if want.get("priceLimit") else None
        return self._create_job(
            id=str(want["id"]),
            title=html_lib.unescape(want["name"]),
            description=html_lib.unescape(want.get("description", "")).replace("\r\n", "\n"),
            url=f"{BASE_URL}/projects/{want['id']}",
            budget=budget,
            currency="RUB" if budget is not None else None,
            posted_at=self._parse_date(want.get("date_active") or want.get("date_create")),
        )

    @staticmethod
    def _parse_date(value: str | None) -> datetime | None:
        if not value:
            return None
        try:
            return datetime.strptime(value, "%Y-%m-%d %H:%M:%S").replace(tzinfo=MSK)
        except ValueError:
            return None
