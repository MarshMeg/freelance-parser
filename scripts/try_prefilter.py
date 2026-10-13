"""РУЧНОЙ запуск префильтра OpenRouter на живых заказах (тратит запросы OpenRouter).

    uv run python scripts/try_prefilter.py [kwork|freelancer|freelance_ru] [N]

Печатает сырой ответ Decisions API на каждый заказ и итоговое решение. В автотесты и CI не включать.
"""
import asyncio
import logging
import sys

sys.path.insert(0, ".")

import httpx  # noqa: E402

import prefilter  # noqa: E402
from config import config  # noqa: E402
from parsers import FreelanceRuParser, FreelancerParser, KworkParser  # noqa: E402

class PrintingTransport(httpx.AsyncHTTPTransport):
    """Печатает сырой ответ OpenRouter — чтобы видеть, что реально возвращает модель"""

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        response = await super().handle_async_request(request)
        body = await response.aread()
        print(f"\n--- HTTP {response.status_code}, модель {config.PREFILTER_MODEL}\n{body.decode()[:3000]}\n")
        return httpx.Response(response.status_code, headers=response.headers, content=body, request=request)


PARSERS = {"kwork": KworkParser, "freelancer": FreelancerParser, "freelance_ru": FreelanceRuParser}


async def main() -> None:
    name = sys.argv[1] if len(sys.argv) > 1 else "kwork"
    n = int(sys.argv[2]) if len(sys.argv) > 2 else 10
    if not config.OPENROUTER_API_KEY:
        raise SystemExit("Нет OPENROUTER_API_KEY в .env")
    logging.basicConfig(level=logging.INFO)
    async with PARSERS[name]() as parser:
        jobs = (await parser.parse())[:n]

    keep, rejected = await prefilter.Prefilter(transport=PrintingTransport()).split(jobs)
    for job in keep:
        print(f"✅ {job.id}: {job.title}")
    for job, reason in rejected:
        print(f"❌ {job.id}: {job.title} — {reason}")


asyncio.run(main())
