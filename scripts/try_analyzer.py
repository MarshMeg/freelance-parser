"""РУЧНОЙ запуск LLM-анализатора на живых заказах (тратит лимиты подписки!).

    uv run python scripts/try_analyzer.py [kwork|freelancer|freelance_ru] [N]

Запускать только владельцу проекта. В автотесты и CI не включать.
"""
import asyncio
import sys

sys.path.insert(0, ".")

from config import config  # noqa: E402
from llm_analyzer import LLMAnalyzer  # noqa: E402
from parsers import FreelanceRuParser, FreelancerParser, KworkParser  # noqa: E402

PARSERS = {"kwork": KworkParser, "freelancer": FreelancerParser, "freelance_ru": FreelanceRuParser}


async def main() -> None:
    name = sys.argv[1] if len(sys.argv) > 1 else "kwork"
    n = int(sys.argv[2]) if len(sys.argv) > 2 else 3
    if not config.ANTHROPIC_API_KEY:
        raise SystemExit("Нет ANTHROPIC_API_KEY в .env")
    async with PARSERS[name]() as parser:
        jobs = (await parser.parse())[:n]
    analyses = await LLMAnalyzer().analyze_many(jobs)
    for job, a in zip(jobs, analyses):
        print(f"\n=== {job.title}\n{job.url}")
        print(a.model_dump_json(indent=2, ensure_ascii=False))


asyncio.run(main())
