import asyncio
import logging

import anthropic
from pydantic import BaseModel, Field

from config import config
from models import DifficultyLevel, Job, JobAnalysis
from utils import format_money

logger = logging.getLogger(__name__)

STACK = config.DEVELOPER_STACK

SYSTEM_PROMPT = f"""Ты помогаешь фрилансеру-разработчику отбирать заказы с бирж.
Стек исполнителя: {STACK}.
Профиль исполнителя тебе не дан: судить о релевантности нужно по стеку выше и здравому смыслу.

Правила:
- is_recommended=true только если заказ технически в рамках стека, описан достаточно ясно для оценки
  и бюджет адекватен объёму работы. Дизайн, копирайтинг, SEO, администрирование, WordPress/PHP/1С
  и прочее вне стека — не рекомендуем.
- relevance_score от 0 до 1: 0 — не подходит вообще, 1 — идеальное совпадение по стеку и условиям.
- difficulty: easy (до дня работы), medium (несколько дней), hard (неделя и больше или высокая неопределённость).
- recommended_price — в валюте заказа; если бюджет задан, ориентируйся на него, но предложи реалистичную
  цену за объём работ. Если оценить нельзя — null.
- response_template — готовый отклик клиенту на языке заказа: коротко, по делу, без воды и шаблонных
  фраз, с конкретикой по задаче и 1–2 уточняющими вопросами. Не выдумывай опыт и кейсы, которых нет.
- reasoning — 1–3 предложения на русском: почему такое решение.
- technologies — технологии, нужные для заказа.
- sonnet_confidence и opus_confidence — от 0 до 1: насколько ты уверен, что заказ целиком, до сдачи
  заказчику, выполнит ИИ-агент для программирования на Claude Sonnet 5.5 и на Claude Opus 5.5
  соответственно; исполнитель только ставит агенту задачу и проверяет результат. Снижай оценку, если
  нужны доступы к чужим системам, работа с железом, созвоны и согласования, ручная работа вне кода,
  визуальный дизайн на вкус заказчика, размытое ТЗ или большой объём. Opus 5.5 сильнее на сложных и
  объёмных задачах, на простых оценки близки; opus_confidence не ниже sonnet_confidence.
- llm_notes — одно предложение на русском: что мешает сделать заказ через LLM или что останется человеку.
- title_ru и description_ru — заполняй, только если is_recommended=true и заказ написан не на русском:
  это перевод названия и описания заказа на русский для карточки на канбан-доске. Переводи полностью,
  без сокращений, пересказа и добавлений; названия технологий, продуктов, код и ссылки оставляй как есть.
  Если заказ уже на русском или не рекомендован — null. Отклик (response_template) не переводи:
  он остаётся на языке заказа."""


class AnalysisResult(BaseModel):
    """Часть JobAnalysis, которую заполняет модель"""
    is_recommended: bool
    difficulty: DifficultyLevel
    relevance_score: float = Field(..., ge=0, le=1)
    recommended_price: float | None = None
    response_template: str
    reasoning: str
    technologies: list[str] = Field(default_factory=list)
    sonnet_confidence: float = Field(..., ge=0, le=1)
    opus_confidence: float = Field(..., ge=0, le=1)
    llm_notes: str
    title_ru: str | None = None
    description_ru: str | None = None


def _build_client() -> anthropic.AsyncAnthropic:
    key = config.ANTHROPIC_API_KEY
    # токен из `claude setup-token` — OAuth (sk-ant-oat...), а не обычный API-ключ
    if key and key.startswith("sk-ant-oat"):
        return anthropic.AsyncAnthropic(
            auth_token=key,
            default_headers={"anthropic-beta": "oauth-2025-04-20"},
            max_retries=3,
        )
    return anthropic.AsyncAnthropic(api_key=key, max_retries=3)


class LLMAnalyzer:
    """Анализ заказов через Claude: релевантность, сложность, цена, отклик"""

    def __init__(
        self,
        client: anthropic.AsyncAnthropic | None = None,
        model: str | None = None,
        concurrency: int | None = None,
    ):
        self.client = client or _build_client()
        self.model = model or config.ANALYZER_MODEL
        self._sem = asyncio.Semaphore(concurrency or config.ANALYZER_CONCURRENCY)

    async def analyze(self, job: Job) -> JobAnalysis:
        async with self._sem:
            # structured outputs: принудительный tool_choice новые модели (Sonnet 5.5 и др.) отклоняют
            response = await self.client.messages.parse(
                model=self.model,
                max_tokens=8000,  # с запасом на перевод длинного описания
                system=SYSTEM_PROMPT,
                messages=[{"role": "user", "content": self._format_job(job)}],
                output_format=AnalysisResult,
            )
        result = response.parsed_output
        if result is None:
            raise ValueError(f"Модель не вернула результат для {job.id} (stop_reason={response.stop_reason})")
        return JobAnalysis(job_id=job.id, source=job.source, **result.model_dump())

    async def analyze_many(self, jobs: list[Job]) -> list[JobAnalysis]:
        """Анализирует заказы параллельно; упавшие логируются и пропускаются"""
        results = await asyncio.gather(*(self.analyze(j) for j in jobs), return_exceptions=True)
        analyses: list[JobAnalysis] = []
        for job, res in zip(jobs, results):
            if isinstance(res, Exception):
                logger.error("Анализ %s не удался: %r", job.id, res)
            else:
                analyses.append(res)
        return analyses

    @staticmethod
    def _format_job(job: Job) -> str:
        budget = format_money(job.budget, job.currency, "не указан")
        return (
            f"Площадка: {job.source}\n"
            f"Название: {job.title}\n"
            f"Бюджет: {budget}\n"
            f"Теги: {', '.join(job.skill_tags) or '—'}\n"
            f"Рейтинг клиента: {job.client_reviews if job.client_reviews is not None else '—'}\n"
            f"Ссылка: {job.url}\n\n"
            f"Описание:\n{job.description[:6000]}"
        )
