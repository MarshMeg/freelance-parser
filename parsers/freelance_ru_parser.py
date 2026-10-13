import logging
import re
from datetime import datetime

from bs4 import BeautifulSoup, Tag

from config import config
from models import Job
from utils import MSK
from .base_parser import BaseParser

logger = logging.getLogger(__name__)

BASE_URL = "https://freelance.ru"
SEARCH_URL = f"{BASE_URL}/task"

_CURRENCIES = {"₽": "RUB", "$": "USD", "€": "EUR"}


class FreelanceRuParser(BaseParser):
    """Парсер ленты заказов freelance.ru"""

    source = "freelance_ru"

    async def parse(self) -> list[Job]:
        html = await self.fetch_page(SEARCH_URL, [("c[]", c) for c in config.FREELANCE_RU_CATEGORIES])
        return self.parse_html(html)

    def parse_html(self, html: str) -> list[Job]:
        soup = BeautifulSoup(html, "html.parser")
        jobs: list[Job] = []
        for card in soup.select("article.task-card"):
            try:
                job = self._parse_card(card)
            except Exception as e:
                logger.warning("Не удалось разобрать карточку freelance.ru: %s", e)
                continue
            if job:
                jobs.append(job)
        return jobs

    def _parse_card(self, card: Tag) -> Job | None:
        link = card.select_one("a.task-card__title-link")
        if not link or not link.get("href"):
            return None
        href = link["href"]
        match = re.search(r"/task/view/(\d+)", href)
        if not match:
            return None

        desc = card.select_one(".task-card__desc")
        description = desc.get_text("\n", strip=True) if desc else ""
        tags = [c.get_text(strip=True) for c in card.select(".task-chip")]
        budget, currency = self._parse_budget(card)

        return self._create_job(
            id=match.group(1),
            title=link.get_text(strip=True),
            description=description,
            url=BASE_URL + href,
            budget=budget,
            currency=currency,
            skill_tags=tags,
            posted_at=self._parse_date(card),
            raw_html=str(card),
        )

    async def enrich(self, job: Job) -> Job:
        """В ленте описание обрезано — берём полный текст со страницы заказа"""
        try:
            html = await self.fetch_page(job.url)
        except Exception as e:
            logger.warning("%s: не удалось загрузить полное описание: %s", job.id, e)
            return job
        full = self.parse_full_description(html)
        return job.model_copy(update={"description": full}) if full else job

    @staticmethod
    def parse_full_description(html: str) -> str:
        """Текст между заголовком и блоком меты в .tv-hero"""
        hero = BeautifulSoup(html, "html.parser").select_one(".tv-hero")
        if not hero or not hero.h1:
            return ""
        parts: list[str] = []
        for node in hero.h1.next_siblings:
            if isinstance(node, Tag):
                if "tv-hero__meta" in node.get("class", []):
                    break
                parts.append("\n" if node.name == "br" else node.get_text())
            else:
                parts.append(str(node))
        return re.sub(r"\n{3,}", "\n\n", "".join(parts)).strip()

    @staticmethod
    def _parse_budget(card: Tag) -> tuple[float | None, str | None]:
        node = card.select_one(".task-card__budget .bold")
        if not node or node.select_one(".cost-not-set") or "cost-not-set" in node.get("class", []):
            return None, None
        text = node.get_text(" ", strip=True)
        digits = re.sub(r"[^\d.,]", "", text).replace(",", ".")
        if not digits:
            return None, None
        currency = next((code for sym, code in _CURRENCIES.items() if sym in text), None)
        return float(digits), currency

    @staticmethod
    def _parse_date(card: Tag) -> datetime | None:
        item = card.select_one(".task-card__foot-item[title]")
        if not item:
            return None
        try:
            return datetime.strptime(item["title"], "%d.%m.%Y %H:%M").replace(tzinfo=MSK)
        except ValueError:
            return None
