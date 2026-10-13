from datetime import datetime
from enum import StrEnum

from pydantic import BaseModel, Field


class DifficultyLevel(StrEnum):
    EASY = "easy"
    MEDIUM = "medium"
    HARD = "hard"


class Job(BaseModel):
    """Заказ с фриланс-биржи"""
    id: str = Field(..., description="Глобальный ID: <source>_<id на бирже>")
    source: str = Field(..., description="Биржа (kwork, freelance_ru, freelancer)")
    title: str = Field(..., description="Название заказа")
    description: str = Field(..., description="Описание заказа")
    budget: float | None = Field(None, description="Бюджет")
    currency: str | None = Field(None, description="Валюта (USD, RUB, ...)")
    skill_tags: list[str] = Field(default_factory=list, description="Теги навыков")
    client_reviews: float | None = Field(None, description="Рейтинг клиента (0-5)")
    url: str = Field(..., description="Ссылка на заказ")
    posted_at: datetime | None = Field(None, description="Время публикации (с часовым поясом)")
    raw_html: str | None = Field(None, description="Исходный HTML для отладки")


class JobAnalysis(BaseModel):
    """Результат анализа заказа LLM"""
    job_id: str
    source: str
    is_recommended: bool
    difficulty: DifficultyLevel
    relevance_score: float = Field(..., ge=0, le=1, description="0-1 оценка релевантности")
    recommended_price: float | None = Field(None, description="Рекомендуемая цена")
    response_template: str = Field(..., description="Черновик ответа клиенту")
    reasoning: str = Field(..., description="Обоснование анализа")
    technologies: list[str] = Field(default_factory=list, description="Определенные технологии")
    sonnet_confidence: float | None = Field(None, ge=0, le=1, description="0-1: задачу решит агент на Sonnet 5.5")
    opus_confidence: float | None = Field(None, ge=0, le=1, description="0-1: задачу решит агент на Opus 5.5")
    llm_notes: str | None = Field(None, description="Что мешает решить задачу через LLM / что останется человеку")
    title_ru: str | None = Field(None, description="Перевод названия на русский (если заказ не на русском)")
    description_ru: str | None = Field(None, description="Перевод описания на русский (если заказ не на русском)")


class GitHubIssuePayload(BaseModel):
    """Данные для создания GitHub Issue"""
    title: str
    body: str
    labels: list[str] = Field(default_factory=list)
