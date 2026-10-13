from datetime import datetime, timezone

import pytest

from config import config
from github_integration import MARKER_RE, GitHubIntegration, build_payload
from models import Job, JobAnalysis
from utils import MSK, parse_timezone

JOB = Job(id="kwork_1", source="kwork", title="Бот", description="Нужен бот", url="https://k/1",
          budget=5000, currency="RUB", posted_at=datetime(2026, 10, 7, 12, 0))


def analysis(**kw):
    base = dict(job_id="kwork_1", source="kwork", is_recommended=True, difficulty="easy",
                relevance_score=0.8, recommended_price=6000, response_template="Привет!\n```py\nx\n```",
                reasoning="ок", technologies=["python"])
    return JobAnalysis(**{**base, **kw})


def test_payload_contents():
    p = build_payload(JOB, analysis())
    assert p.title == "[Kwork] Бот"
    assert MARKER_RE.search(p.body).group(1) == "kwork_1"
    assert "6000 RUB" in p.body and "https://k/1" in p.body
    assert "````" in p.body  # fence длиннее вложенного ```
    assert {"freelance", "kwork", "recommended", "difficulty:easy"} <= set(p.labels)


def test_payload_uses_russian_translation_and_keeps_original():
    job = JOB.model_copy(update={"title": "Telegram bot", "description": "Need a bot"})
    p = build_payload(job, analysis(title_ru="Телеграм-бот", description_ru="Нужен бот"))
    assert p.title == "[Kwork] Телеграм-бот"
    described, original = p.body.split("<summary>Оригинал</summary>")
    assert "Нужен бот" in described and "Need a bot" not in described
    assert "**Telegram bot**" in original and "Need a bot" in original
    assert "Оригинал" not in build_payload(JOB, analysis()).body  # без перевода блока нет


def test_payload_shows_llm_confidence():
    p = build_payload(JOB, analysis(sonnet_confidence=0.65, opus_confidence=0.9, llm_notes="Нужен доступ к серверу"))
    assert "## Решаемость через LLM" in p.body
    assert "Claude Sonnet 5.5: 65%" in p.body and "Claude Opus 5.5: 90%" in p.body
    assert "Нужен доступ к серверу" in p.body
    assert p.body.index("Решаемость через LLM") < p.body.index("Черновик отклика")
    assert "Решаемость через LLM" not in build_payload(JOB, analysis()).body


def test_payload_not_recommended_has_no_label():
    assert "recommended" not in build_payload(JOB, analysis(is_recommended=False)).labels


def test_payload_label_is_configurable(monkeypatch):
    monkeypatch.setattr(config, "GITHUB_ISSUE_LABEL", "jobs")
    labels = build_payload(JOB, analysis()).labels
    assert labels[0] == "jobs" and "freelance" not in labels


def project_node(option):
    return {"user": {"projectV2": {"id": "P", "fields": {"nodes": [
        {}, {"id": "F", "name": "Status", "options": [{"id": "O", "name": option}]},
    ]}}}}


def test_load_project_finds_configured_status_option(monkeypatch):
    gh = GitHubIntegration.__new__(GitHubIntegration)
    gh._project = None
    gh._graphql = lambda query, variables: project_node("Inbox")
    with pytest.raises(RuntimeError, match="Backlog"):  # по умолчанию ищется Status / Backlog
        gh._load_project()
    monkeypatch.setattr(config, "GITHUB_PROJECT_STATUS_OPTION", "Inbox")
    assert gh._load_project() == {"id": "P", "field": "F", "option": "O"}


def test_publish_skips_known_and_not_recommended():
    gh = GitHubIntegration.__new__(GitHubIntegration)
    gh._known = {"kwork_1"}
    calls = []
    gh.create_issue = lambda job, a: calls.append(job.id) or 1
    other = JOB.model_copy(update={"id": "kwork_2"})
    third = JOB.model_copy(update={"id": "kwork_3"})
    out = gh.publish([(JOB, analysis()), (other, analysis()), (third, analysis(is_recommended=False))])
    assert calls == ["kwork_2"] and out == [1]


def test_payload_date_in_configured_timezone():
    job = JOB.model_copy(update={"posted_at": datetime(2026, 10, 7, 9, 0, tzinfo=timezone.utc)})
    assert "Опубликован: 2026-10-07 13:00 UTC+04:00" in build_payload(job, analysis(), parse_timezone("UTC+4")).body
    assert "Опубликован: 2026-10-07 12:00 MSK" in build_payload(job, analysis(), MSK).body


@pytest.mark.parametrize("value, offset", [
    ("UTC+4", 4), ("+04:00", 4), ("gmt-3", -3), ("+0530", 5.5), ("UTC", 0), ("Europe/Samara", 4),
])
def test_parse_timezone(value, offset):
    tz = parse_timezone(value)
    assert datetime(2026, 10, 7, tzinfo=tz).utcoffset().total_seconds() == offset * 3600


def test_parse_timezone_rejects_garbage():
    with pytest.raises(ValueError):
        parse_timezone("Mars/Olympus")


def test_payload_without_date():
    job = JOB.model_copy(update={"posted_at": None})
    assert "Опубликован: —" in build_payload(job, analysis()).body
