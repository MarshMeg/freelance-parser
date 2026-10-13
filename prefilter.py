"""Дешёвый предварительный отсев заказов через OpenRouter перед анализом в Claude.

Модель по умолчанию — Jev (`~typesafe/jev-latest`): это не чат-модель, а decision-модель.
Ей шлют заказ (`state`) и типизированный вопрос, в ответ приходит вероятность «да», без текста.
Через chat/completions она не работает — только через Decisions API.

Префильтр не должен терять заказы: при любой ошибке или непонятном ответе заказ пропускается дальше.
"""
import asyncio
import logging

import httpx

from config import config
from llm_analyzer import STACK
from models import Job
from utils import format_money

logger = logging.getLogger(__name__)

API_URL = "https://openrouter.ai/api/alpha/decisions"
CONCURRENCY = 8
DESCRIPTION_CHARS = 1500

QUESTION = "out_of_scope"
QUESTIONS = {
    QUESTION: {
        "type": "noul",
        "instructions": (
            "The state is a job posting from a freelance marketplace. "
            f"The freelancer is a software developer; their stack: {STACK}. "
            "Is this job clearly outside what this developer does?"
        ),
        "criteria": {
            "true": (
                "The job is clearly not software development or clearly needs another stack: design, "
                "copywriting, reviews, SEO, SMM, translation, system administration, "
                "WordPress/PHP/1C/Bitrix, native mobile development and the like."
            ),
            "false": (
                "The job is software development that may fit the stack, or it is unclear: "
                "the description is short, vague or mixed."
            ),
        },
    }
}


def _state(job: Job) -> dict:
    return {
        "title": job.title,
        "budget": format_money(job.budget, job.currency, "не указан"),
        "tags": job.skill_tags,
        "description": " ".join(job.description.split())[:DESCRIPTION_CHARS],
    }


def parse_probability(data: object) -> float | None:
    """Ответ Decisions API -> вероятность «заказ вне стека»; непонятное -> None"""
    try:
        value = data["answers"][QUESTION]["noul"]
    except (KeyError, TypeError):
        return None
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not 0 <= value <= 1:
        return None
    return float(value)


class Prefilter:
    def __init__(
        self,
        api_key: str | None = None,
        model: str | None = None,
        threshold: float | None = None,
        transport: httpx.AsyncBaseTransport | None = None,
    ):
        self.api_key = api_key or config.OPENROUTER_API_KEY
        self.model = model or config.PREFILTER_MODEL
        self.threshold = config.PREFILTER_THRESHOLD if threshold is None else threshold
        self._transport = transport  # для тестов

    async def split(self, jobs: list[Job]) -> tuple[list[Job], list[tuple[Job, str]]]:
        """(заказы для Claude, отсеянные с причиной)"""
        sem = asyncio.Semaphore(CONCURRENCY)
        async with httpx.AsyncClient(
            timeout=60,
            transport=self._transport,
            headers={"Authorization": f"Bearer {self.api_key}", "X-Title": "freelance-parser"},
        ) as client:
            async def decide(job: Job) -> float:
                async with sem:
                    return await self._decide(client, job)

            results = await asyncio.gather(*(decide(j) for j in jobs), return_exceptions=True)

        keep: list[Job] = []
        rejected: list[tuple[Job, str]] = []
        errors = [r for r in results if isinstance(r, Exception)]
        for job, res in zip(jobs, results):
            if not isinstance(res, Exception) and res >= self.threshold:
                rejected.append((job, f"вне стека, вероятность {res:.2f}"))
            else:
                keep.append(job)
        if errors:
            # одна строка на запуск: при неверной модели или ключе ошибка у всех заказов одинаковая
            logger.warning("Префильтр не ответил по %d из %d заказов — они идут дальше; первая ошибка: %r",
                           len(errors), len(jobs), errors[0])
        return keep, rejected

    async def _decide(self, client: httpx.AsyncClient, job: Job) -> float:
        response = await client.post(API_URL, json={
            "model": self.model,
            "state": _state(job),
            "questions": QUESTIONS,
        })
        if response.is_error:
            # в теле OpenRouter объясняет причину (неизвестная модель, лимиты и т. п.)
            raise RuntimeError(f"OpenRouter ответил {response.status_code}: {response.text[:500]}")
        probability = parse_probability(response.json())
        if probability is None:
            raise ValueError(f"Непонятный ответ OpenRouter по {job.id}: {response.text[:300]}")
        return probability
