import logging
from datetime import datetime, timezone

from config import config
from models import Job
from .base_parser import BaseParser

logger = logging.getLogger(__name__)

BASE_URL = "https://www.freelancer.com"
API_URL = f"{BASE_URL}/api/projects/0.1/projects/active/"


class FreelancerParser(BaseParser):
    """Парсер freelancer.com через публичный API проектов"""

    source = "freelancer"

    def __init__(self, timeout: int = 30, skill_ids: list[int] | None = None, limit: int = 50):
        super().__init__(timeout)
        self.skill_ids = skill_ids or config.FREELANCER_SKILL_IDS
        self.limit = limit

    async def parse(self) -> list[Job]:
        params = [
            ("limit", self.limit),
            ("full_description", "true"),
            ("job_details", "true"),
            ("compact", "true"),
            *(("jobs[]", i) for i in self.skill_ids),
        ]
        response = await self.fetch(API_URL, params)
        return self.parse_json(response.json())

    def parse_json(self, data: dict) -> list[Job]:
        jobs: list[Job] = []
        for project in data.get("result", {}).get("projects", []):
            try:
                jobs.append(self._parse_project(project))
            except Exception as e:
                logger.warning("Не удалось разобрать проект freelancer %s: %s", project.get("id"), e)
        return jobs

    def _parse_project(self, p: dict) -> Job:
        budget = p.get("budget") or {}
        currency = (p.get("currency") or {}).get("code")
        description = p.get("description", "")
        if budget:
            unit = "/h" if p.get("type") == "hourly" else ""
            description = (
                f"[{p.get('type', 'fixed')} project, budget {budget.get('minimum')}-"
                f"{budget.get('maximum')} {currency}{unit}]\n{description}"
            )
        posted = p.get("time_submitted")
        return self._create_job(
            id=str(p["id"]),
            title=p["title"],
            description=description,
            url=f"{BASE_URL}/projects/{p['seo_url']}",
            budget=budget.get("maximum"),
            currency=currency,
            skill_tags=[j["name"] for j in p.get("jobs", [])],
            posted_at=datetime.fromtimestamp(posted, timezone.utc) if posted else None,
        )
