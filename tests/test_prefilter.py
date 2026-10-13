import asyncio
import json
from datetime import datetime
from types import SimpleNamespace as NS

import httpx

import main
import prefilter
from models import Job, JobAnalysis
from prefilter import Prefilter, parse_probability
from utils import SeenStore


def job(i):
    return Job(id=f"kwork_{i}", source="kwork", title=f"t{i}", description="d " * 500, url="u",
               posted_at=datetime(2026, 10, 7, 12))


def reply(probability):
    return httpx.Response(200, json={"answers": {"out_of_scope": {"type": "noul", "noul": probability}}})


def make(handler, requests=None):
    def wrapped(request):
        if requests is not None:
            requests.append((str(request.url), json.loads(request.content)))
        return handler(request)
    return Prefilter(api_key="k", model="m", threshold=0.8, transport=httpx.MockTransport(wrapped))


def by_title(probabilities):
    """Ответ зависит от заказа в запросе: {title: вероятность «вне стека»}"""
    return lambda request: reply(probabilities[json.loads(request.content)["state"]["title"]])


def test_parse_probability_rejects_garbage():
    assert parse_probability({"answers": {"out_of_scope": {"type": "noul", "noul": 0.25}}}) == 0.25
    for data in ({}, {"error": "x"}, {"answers": {}}, {"answers": {"out_of_scope": {"noul": "0.9"}}},
                 {"answers": {"out_of_scope": {"noul": True}}}, {"answers": {"out_of_scope": {"noul": 1.5}}}, None):
        assert parse_probability(data) is None


def test_split_rejects_only_above_threshold_via_decisions_api():
    requests = []
    jobs = [job(i) for i in range(1, 4)]
    keep, rejected = asyncio.run(make(by_title({"t1": 0.95, "t2": 0.79, "t3": 0.8}), requests).split(jobs))
    assert [j.id for j in keep] == ["kwork_2"]
    assert [(j.id, r) for j, r in rejected] == [("kwork_1", "вне стека, вероятность 0.95"),
                                                ("kwork_3", "вне стека, вероятность 0.80")]
    assert len(requests) == 3  # по запросу на заказ
    url, body = requests[0]
    assert url == "https://openrouter.ai/api/alpha/decisions"
    assert body["model"] == "m" and body["questions"]["out_of_scope"]["type"] == "noul"
    assert "messages" not in body and len(body["state"]["description"]) <= prefilter.DESCRIPTION_CHARS


def test_split_fails_open_on_errors(caplog):
    jobs = [job(1), job(2)]
    for handler in (
        lambda r: httpx.Response(500),
        lambda r: httpx.Response(400, json={"error": {"message": "not a decisions model"}}),
        lambda r: httpx.Response(200, json={"error": "x"}),
        lambda r: httpx.Response(200, text="не JSON"),
    ):
        keep, rejected = asyncio.run(make(handler).split(jobs))
        assert keep == jobs and rejected == []
    assert "not a decisions model" in caplog.text  # тело ответа OpenRouter видно в логе


def test_split_keeps_only_failed_jobs_on_partial_failure():
    def handler(request):
        if json.loads(request.content)["state"]["title"] == "t1":
            return httpx.Response(500)
        return reply(0.99)

    keep, rejected = asyncio.run(make(handler).split([job(1), job(2)]))
    assert [j.id for j in keep] == ["kwork_1"] and [j.id for j, _ in rejected] == ["kwork_2"]


def test_run_skips_rejected_and_remembers_them(tmp_path):
    seen = SeenStore(str(tmp_path / "s.json"), 30)
    analyzed = []

    class A:
        async def analyze_many(self, jobs):
            analyzed.extend(j.id for j in jobs)
            return [JobAnalysis(job_id=j.id, source=j.source, is_recommended=False, difficulty="easy",
                                relevance_score=0.1, response_template="x", reasoning="r") for j in jobs]

    class P:
        source = "kwork"
        async def __aenter__(self): return self
        async def __aexit__(self, *a): pass
        async def parse(self): return [job(1), job(2)]
        async def enrich(self, j): return j

    pf = make(by_title({"t1": 0.95, "t2": 0.1}))
    gh = NS(known_job_ids=lambda: set(), publish=lambda pairs: [])
    asyncio.run(main.run([P()], A(), gh, seen, limit=0, prefilter=pf))
    assert analyzed == ["kwork_2"]
    assert "kwork_1" in SeenStore(str(tmp_path / "s.json"), 30)
