import argparse
import asyncio
import logging

from config import config
from github_integration import GitHubIntegration
from llm_analyzer import LLMAnalyzer
from models import Job
from parsers import BaseParser, FreelanceRuParser, FreelancerParser, KworkParser
from prefilter import Prefilter
from utils import SeenStore, setup_logging

logger = logging.getLogger("main")

# заказы анализируются партиями: после каждой кэш сохраняется и Issues создаются,
# так что прерванный запуск не теряет уже сделанный (и оплаченный лимитами) анализ
BATCH_SIZE = 10


PARSERS: dict[str, type[BaseParser]] = {
    p.source: p for p in (KworkParser, FreelanceRuParser, FreelancerParser)
}


def default_parsers(enabled: list[str] | None = None) -> list[BaseParser]:
    """Парсеры из ENABLED_PARSERS; опечатка в имени — ошибка, а не молча выключенный источник"""
    names = list(dict.fromkeys(config.ENABLED_PARSERS if enabled is None else enabled))
    unknown = [n for n in names if n not in PARSERS]
    if unknown:
        raise ValueError(f"Неизвестные парсеры в ENABLED_PARSERS: {', '.join(unknown)} (есть: {', '.join(PARSERS)})")
    if not names:
        raise ValueError("ENABLED_PARSERS пуст: не включён ни один парсер")
    return [PARSERS[n](config.PARSER_TIMEOUT) for n in names]


def _label(job: Job) -> str:
    """Заказ для лога: ID и, если разрешено LOG_JOB_TITLES, название"""
    return f"{job.id}: {job.title}" if config.LOG_JOB_TITLES else job.id


async def _parse_one(parser: BaseParser) -> list[Job]:
    try:
        async with parser:
            jobs = await parser.parse()
        logger.info("%s: найдено %d", parser.source, len(jobs))
        return jobs
    except Exception as e:
        logger.error("%s: парсер упал: %r", parser.source, e)
        return []


async def enrich_jobs(parsers: list[BaseParser], jobs: list[Job]) -> list[Job]:
    """Догрузить полные описания через парсер-источник (ошибки не критичны)"""
    by_source = {p.source: p for p in parsers}
    result: dict[str, Job] = {}

    async def one(parser: BaseParser, source_jobs: list[Job]) -> None:
        async with parser:
            done = await asyncio.gather(*(parser.enrich(j) for j in source_jobs), return_exceptions=True)
        for job, res in zip(source_jobs, done):
            if isinstance(res, Exception):
                logger.warning("%s: enrich не удался: %s", job.id, res)
            else:
                result[job.id] = res

    groups = {src: [j for j in jobs if j.source == src] for src in by_source}
    await asyncio.gather(*(one(by_source[src], js) for src, js in groups.items() if js))
    return [result.get(j.id, j) for j in jobs]


async def collect_jobs(parsers: list[BaseParser]) -> list[Job]:
    """Запустить парсеры параллельно; упавший парсер не ломает остальные"""
    results = await asyncio.gather(*(_parse_one(p) for p in parsers))
    seen_ids: set[str] = set()
    jobs: list[Job] = []
    for job in (j for batch in results for j in batch):
        if job.id not in seen_ids:
            seen_ids.add(job.id)
            jobs.append(job)
    return jobs


def select_new(jobs: list[Job], seen: SeenStore, known: set[str], limit: int) -> list[Job]:
    """Отбросить уже виденные, самые свежие первыми; limit > 0 — не больше limit"""
    fresh = [j for j in jobs if j.id not in seen and j.id not in known]
    fresh.sort(key=lambda j: j.posted_at.timestamp() if j.posted_at else 0, reverse=True)
    return fresh[:limit] if limit > 0 else fresh


async def run(
    parsers: list[BaseParser],
    analyzer: LLMAnalyzer | None,
    github: GitHubIntegration | None,
    seen: SeenStore,
    limit: int,
    dry_run: bool = False,
    prefilter: Prefilter | None = None,
) -> list[int]:
    jobs = await collect_jobs(parsers)
    known = await asyncio.to_thread(github.known_job_ids) if github else set()
    new_jobs = select_new(jobs, seen, known, limit)
    logger.info("Всего %d, новых к анализу %d", len(jobs), len(new_jobs))

    if dry_run or not new_jobs:
        for j in new_jobs:
            logger.info("[dry-run] %s", _label(j))
        return []

    if prefilter:
        new_jobs, rejected = await prefilter.split(new_jobs)
        for job, reason in rejected:
            logger.info("[prefilter] отсеян %s — %s", _label(job), reason)
            seen.add(job.id)
        seen.save()
        logger.info("Префильтр: отсеяно %d, к анализу %d", len(rejected), len(new_jobs))

    analyzed = recommended = 0
    created: list[int] = []
    for start in range(0, len(new_jobs), BATCH_SIZE):
        batch = await enrich_jobs(parsers, new_jobs[start:start + BATCH_SIZE])
        analyses = await analyzer.analyze_many(batch)
        by_id = {j.id: j for j in batch}
        pairs = [(by_id[a.job_id], a) for a in analyses]
        # упавшие в анализе не помечаются — попробуем в следующий запуск
        for job, _ in pairs:
            seen.add(job.id)
        seen.save()
        created += await asyncio.to_thread(github.publish, pairs)
        analyzed += len(pairs)
        recommended += sum(a.is_recommended for _, a in pairs)
        logger.info("Обработано %d/%d", min(start + BATCH_SIZE, len(new_jobs)), len(new_jobs))

    logger.info(
        "Проанализировано %d, рекомендовано %d, создано Issues: %d",
        analyzed, recommended, len(created),
    )
    return created


async def amain(dry_run: bool) -> None:
    setup_logging(config.LOG_LEVEL)
    if not dry_run:
        config.validate()
    seen = SeenStore(config.SEEN_FILE, config.CACHE_DAYS)
    github = GitHubIntegration() if config.GITHUB_TOKEN else None
    analyzer = None if dry_run else LLMAnalyzer()
    prefilter = Prefilter() if config.OPENROUTER_API_KEY and not dry_run else None
    try:
        parsers = default_parsers()
        logger.info("Активные парсеры: %s", ", ".join(p.source for p in parsers))
        await run(parsers, analyzer, github, seen, config.MAX_JOBS_PER_RUN, dry_run, prefilter)
    finally:
        if github:
            github.close()


def main() -> None:
    ap = argparse.ArgumentParser(description="Парсер фриланс-бирж → LLM → GitHub Project")
    ap.add_argument("--dry-run", action="store_true", help="только парсинг и отбор новых, без LLM и Issues")
    asyncio.run(amain(ap.parse_args().dry_run))


if __name__ == "__main__":
    main()
