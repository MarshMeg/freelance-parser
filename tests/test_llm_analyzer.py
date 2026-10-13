import asyncio
from datetime import datetime
from types import SimpleNamespace as NS

import llm_analyzer
import prefilter
from config import config
from llm_analyzer import AnalysisResult, LLMAnalyzer
from models import Job


def job(i):
    return Job(id=f"kwork_{i}", source="kwork", title=f"t{i}", description="d", url="u",
               posted_at=datetime(2026, 10, 7, 12))


def result(**kw):
    return AnalysisResult(is_recommended=True, difficulty="easy", relevance_score=0.9,
                          response_template="r", reasoning="why", sonnet_confidence=0.7,
                          opus_confidence=0.9, llm_notes="нужен доступ к серверу", **kw)


class FakeMessages:
    def __init__(self, replies):
        self.replies, self.calls = list(replies), []

    async def parse(self, **kwargs):
        self.calls.append(kwargs)
        reply = self.replies.pop(0)
        if isinstance(reply, Exception):
            raise reply
        return reply


def make(*replies):
    messages = FakeMessages(replies)
    return LLMAnalyzer(client=NS(messages=messages), model="m", concurrency=1), messages


def test_analyze_uses_structured_output_without_forced_tool():
    analyzer, messages = make(NS(parsed_output=result(technologies=["Go"]), stop_reason="end_turn"))
    analysis = asyncio.run(analyzer.analyze(job(1)))
    assert (analysis.job_id, analysis.source, analysis.technologies) == ("kwork_1", "kwork", ["Go"])
    call = messages.calls[0]
    assert call["model"] == "m" and call["output_format"] is AnalysisResult
    assert "tool_choice" not in call and "tools" not in call


def test_analyze_passes_translation_through():
    analyzer, _ = make(NS(parsed_output=result(title_ru="Бот", description_ru="Нужен бот"), stop_reason="end_turn"))
    analysis = asyncio.run(analyzer.analyze(job(1)))
    assert (analysis.title_ru, analysis.description_ru) == ("Бот", "Нужен бот")
    assert (analysis.sonnet_confidence, analysis.opus_confidence) == (0.7, 0.9)


def test_analyze_many_skips_failed_and_empty():
    analyzer, _ = make(
        RuntimeError("boom"),
        NS(parsed_output=None, stop_reason="refusal"),
        NS(parsed_output=result(), stop_reason="end_turn"),
    )
    analyses = asyncio.run(analyzer.analyze_many([job(1), job(2), job(3)]))
    assert [a.job_id for a in analyses] == ["kwork_3"]


def test_stack_in_prompts_comes_from_config():
    assert config.DEVELOPER_STACK.startswith("Go, TypeScript, JavaScript, Python")
    assert f"Стек исполнителя: {config.DEVELOPER_STACK}." in llm_analyzer.SYSTEM_PROMPT
    assert config.DEVELOPER_STACK in prefilter.QUESTIONS[prefilter.QUESTION]["instructions"]
