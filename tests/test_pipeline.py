import asyncio
import time
import json
from datetime import datetime, timezone
from types import SimpleNamespace as NS

import main
from config import config
from models import Job, JobAnalysis
from parsers import FreelanceRuParser, FreelancerParser, KworkParser
from utils import MSK, SeenStore


def job(i, hour=12):
    return Job(id=f"kwork_{i}", source="kwork", title=f"t{i}", description="d", url="u",
               posted_at=datetime(2026, 10, 7, hour))


class FakeParser:
    def __init__(self, source, jobs=None, fail=False):
        self.source, self._jobs, self._fail = source, jobs or [], fail

    async def __aenter__(self): return self
    async def __aexit__(self, *a): pass

    async def parse(self):
        if self._fail:
            raise RuntimeError("boom")
        return self._jobs


def test_seen_store_roundtrip_and_ttl(tmp_path):
    path = tmp_path / "s.json"
    s = SeenStore(str(path), 1)
    s.add("a")
    s._data["old"] = time.time() - 2 * 86400
    s.save()
    s2 = SeenStore(str(path), 1)
    assert "a" in s2 and "old" not in s2


def test_collect_survives_failed_parser_and_dedups():
    jobs = asyncio.run(main.collect_jobs([
        FakeParser("a", [job(1), job(2)]), FakeParser("b", fail=True), FakeParser("c", [job(2)]),
    ]))
    assert [j.id for j in jobs] == ["kwork_1", "kwork_2"]


def test_select_new_filters_sorts_limits(tmp_path):
    seen = SeenStore(str(tmp_path / "s.json"), 1)
    seen.add("kwork_1")
    jobs = [job(1), job(2, 10), job(3, 15), job(4, 11), job(5, 9)]
    out = main.select_new(jobs, seen, {"kwork_5"}, limit=2)
    assert [j.id for j in out] == ["kwork_3", "kwork_4"]


def test_run_end_to_end_with_fakes(tmp_path):
    seen = SeenStore(str(tmp_path / "s.json"), 1)
    published = []

    class A:
        async def analyze_many(self, jobs):
            return [JobAnalysis(job_id=j.id, source=j.source, is_recommended=True, difficulty="easy",
                                relevance_score=0.9, response_template="x", reasoning="r") for j in jobs]

    gh = NS(known_job_ids=lambda: set(),
            publish=lambda pairs: published.extend(p[0].id for p in pairs) or [1, 2])
    created = asyncio.run(main.run([FakeParser("a", [job(1), job(2)])], A(), gh, seen, limit=10))
    assert created == [1, 2] and set(published) == {"kwork_1", "kwork_2"}
    assert "kwork_1" in seen and (tmp_path / "s.json").exists()


def test_select_new_without_limit_returns_all(tmp_path):
    seen = SeenStore(str(tmp_path / "s.json"), 1)
    seen.add("kwork_1")
    out = main.select_new([job(i) for i in range(1, 6)], seen, {"kwork_2"}, limit=0)
    assert sorted(j.id for j in out) == ["kwork_3", "kwork_4", "kwork_5"]


def test_run_processes_in_batches_and_never_repeats(tmp_path, monkeypatch):
    """Все новые заказы обрабатываются партиями, кэш сохраняется после каждой,
    упавшие в анализе повторяются, а уже проанализированные — никогда"""
    monkeypatch.setattr(main, "BATCH_SIZE", 2)
    path = tmp_path / "s.json"
    analyzed, saved_sizes = [], []

    class A:
        async def analyze_many(self, jobs):
            analyzed.extend(j.id for j in jobs)
            saved_sizes.append(len(json.loads(path.read_text())) if path.exists() else 0)
            return [JobAnalysis(job_id=j.id, source=j.source, is_recommended=False, difficulty="easy",
                                relevance_score=0.1, response_template="x", reasoning="r")
                    for j in jobs if j.id != "kwork_5"]  # kwork_5 «упал»

    gh = NS(known_job_ids=lambda: set(), publish=lambda pairs: [])
    jobs = [job(i) for i in range(1, 6)]
    asyncio.run(main.run([FakeParser("a", jobs)], A(), gh, SeenStore(str(path), 30), limit=0))
    assert sorted(analyzed) == [f"kwork_{i}" for i in range(1, 6)]
    assert saved_sizes == [0, 2, 4]  # кэш пишется после каждой партии

    analyzed.clear()
    asyncio.run(main.run([FakeParser("a", jobs)], A(), gh, SeenStore(str(path), 30), limit=0))
    assert analyzed == ["kwork_5"]


def test_dry_run_does_not_call_llm_or_github(tmp_path):
    seen = SeenStore(str(tmp_path / "s.json"), 1)
    gh = NS(known_job_ids=lambda: set())
    out = asyncio.run(main.run([FakeParser("a", [job(1)])], None, gh, seen, limit=5, dry_run=True))
    assert out == [] and "kwork_1" not in seen


def test_dry_run_log_hides_titles_when_disabled(tmp_path, monkeypatch, caplog):
    caplog.set_level("INFO")
    gh = NS(known_job_ids=lambda: set())
    for enabled, expected in ((True, "[dry-run] kwork_1: t1"), (False, "[dry-run] kwork_1")):
        monkeypatch.setattr(config, "LOG_JOB_TITLES", enabled)
        caplog.clear()
        seen = SeenStore(str(tmp_path / "s.json"), 1)
        asyncio.run(main.run([FakeParser("a", [job(1)])], None, gh, seen, limit=5, dry_run=True))
        assert caplog.messages[-1] == expected


def test_enrich_jobs_replaces_description_and_survives_errors():
    class P(FakeParser):
        async def enrich(self, j):
            if j.id == "kwork_2":
                raise RuntimeError("x")
            return j.model_copy(update={"description": "FULL"})

    out = asyncio.run(main.enrich_jobs([P("kwork")], [job(1), job(2)]))
    assert [j.description for j in out] == ["FULL", "d"]


def test_select_new_compares_across_timezones(tmp_path):
    seen = SeenStore(str(tmp_path / "s.json"), 1)
    msk = job(1).model_copy(update={"posted_at": datetime(2026, 10, 7, 14, 0, tzinfo=MSK)})  # 11:00 UTC
    utc = job(2).model_copy(update={"posted_at": datetime(2026, 10, 7, 12, 0, tzinfo=timezone.utc)})
    assert [j.id for j in main.select_new([msk, utc], seen, set(), limit=1)] == ["kwork_2"]


def test_kwork_parse_html():
    state = {"pagination": {"last_page": 1, "data": [
        {"id": 7, "name": "Бот &amp; парсер", "description": "a\r\nb", "priceLimit": "3000.00",
         "date_create": "2026-10-07 15:00:00"},
        {"name": "без id"},
    ]}}
    html = f'<script>window.x = {{"wantsListData":{json.dumps(state)}}};</script>'
    [j] = KworkParser().parse_html(html)
    assert (j.id, j.title, j.description, j.budget, j.currency) == ("kwork_7", "Бот & парсер", "a\nb", 3000, "RUB")
    assert j.posted_at == datetime(2026, 10, 7, 12, 0, tzinfo=timezone.utc)


def test_freelancer_parse_json():
    data = {"result": {"projects": [{
        "id": 5, "title": "API", "description": "d", "seo_url": "python/api", "type": "fixed",
        "budget": {"minimum": 100, "maximum": 300}, "currency": {"code": "USD"},
        "jobs": [{"name": "Python"}], "time_submitted": 1791374400,
    }]}}
    [j] = FreelancerParser().parse_json(data)
    assert (j.id, j.budget, j.currency, j.skill_tags) == ("freelancer_5", 300, "USD", ["Python"])
    assert j.description.startswith("[fixed project, budget 100-300 USD]")
    assert j.posted_at == datetime.fromtimestamp(1791374400, timezone.utc)


def test_kwork_and_freelancer_filters_come_from_config(monkeypatch):
    assert (KworkParser().category_id, FreelancerParser().skill_ids) == (41, [13, 9, 759, 1093, 1612, 305])
    monkeypatch.setattr(config, "KWORK_CATEGORY_ID", 0)
    monkeypatch.setattr(config, "FREELANCER_SKILL_IDS", [13])
    assert (KworkParser().category_id, FreelancerParser().skill_ids) == (0, [13])
    assert KworkParser(category_id=11).category_id == 11


def test_freelance_ru_requests_configured_categories(monkeypatch):
    monkeypatch.setattr(config, "FREELANCE_RU_CATEGORIES", ["4", "724"])
    requested = []

    async def fake_fetch_page(self, url, params=None):
        requested.append((url, params))
        return "<html></html>"

    monkeypatch.setattr(FreelanceRuParser, "fetch_page", fake_fetch_page)
    assert asyncio.run(FreelanceRuParser().parse()) == []
    assert requested == [("https://freelance.ru/task", [("c[]", "4"), ("c[]", "724")])]


def test_freelance_ru_full_description():
    html = '<section class="tv-hero"><h1>T</h1>\n Строка 1<br/>\nСтрока 2<div class="tv-hero__meta">x</div></section>'
    assert FreelanceRuParser.parse_full_description(html) == "Строка 1\n\nСтрока 2"


def test_default_parsers_follow_enabled_list(monkeypatch):
    monkeypatch.setattr(config, "ENABLED_PARSERS", ["kwork", "freelance_ru"])
    assert [p.source for p in main.default_parsers()] == ["kwork", "freelance_ru"]
    assert [p.source for p in main.default_parsers(["freelancer", "freelancer"])] == ["freelancer"]


def test_default_parsers_reject_unknown_and_empty():
    for bad in (["kwork", "upwork"], []):
        try:
            main.default_parsers(bad)
        except ValueError:
            continue
        raise AssertionError(f"нет ошибки для {bad}")
