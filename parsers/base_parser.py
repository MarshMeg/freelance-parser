from abc import ABC, abstractmethod
from datetime import datetime

import httpx

from models import Job

DEFAULT_HEADERS = {"User-Agent": "Mozilla/5.0"}


class BaseParser(ABC):
    """Базовый класс парсеров фриланс-бирж.

    Используется как async-контекстный менеджер; HTTP-клиент создаётся на входе,
    поэтому один парсер можно открыть повторно (parse, затем enrich).
    """

    source: str  # префикс Job.id и значение Job.source

    def __init__(self, timeout: int = 30):
        self.timeout = timeout
        self.client: httpx.AsyncClient | None = None

    async def __aenter__(self):
        if self.client is None or self.client.is_closed:
            self.client = httpx.AsyncClient(
                timeout=self.timeout, follow_redirects=True, headers=DEFAULT_HEADERS
            )
        return self

    async def __aexit__(self, exc_type, exc_val, exc_tb):
        await self.client.aclose()

    @abstractmethod
    async def parse(self) -> list[Job]:
        """Загрузить и разобрать ленту заказов"""

    async def enrich(self, job: Job) -> Job:
        """Догрузить полные данные заказа (по умолчанию — ничего не делает).

        Вызывается только для заказов, которые пойдут в LLM-анализ.
        """
        return job

    async def fetch(self, url: str, params: dict | list | None = None) -> httpx.Response:
        """GET с проверкой статуса (ошибки логирует вызывающий код)"""
        response = await self.client.get(url, params=params)
        response.raise_for_status()
        return response

    async def fetch_page(self, url: str, params: dict | list | None = None) -> str:
        return (await self.fetch(url, params)).text

    def _create_job(
        self,
        id: str,
        title: str,
        description: str,
        url: str,
        budget: float | None = None,
        currency: str | None = None,
        skill_tags: list[str] | None = None,
        client_reviews: float | None = None,
        posted_at: datetime | None = None,
        raw_html: str | None = None,
    ) -> Job:
        """Job с id вида <source>_<id>"""
        return Job(
            id=f"{self.source}_{id}",
            source=self.source,
            title=title,
            description=description,
            url=url,
            budget=budget,
            currency=currency,
            skill_tags=skill_tags or [],
            client_reviews=client_reviews,
            posted_at=posted_at,
            raw_html=raw_html,
        )
