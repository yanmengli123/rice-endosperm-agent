"""追问建议（followup_suggestions）生成与出口校验的单测。

接线（终态区生成 → usage 并入 run 总量 → extra_metadata 落库 → SSE chunk）由
契约语料与 e2e 覆盖；这里聚焦纯函数与生成函数的失败降级语义：任何失败都
返回 None，不影响 finished 终态。
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest

from yuxi.services.chat_service import (
    _FOLLOWUP_QUESTION_COUNT,
    _generate_followup_suggestions,
    _sanitize_followup_questions,
)

pytestmark = [pytest.mark.unit]

LONG_QUESTION = "这是一个特别长的追问" * 12


class _FakeChunkModel:
    """astream 逐段产出文本，末段携带 usage_metadata（LangChain 惯例）。"""

    def __init__(self, pieces: list[str], usage: dict | None = None, delay: float = 0.0):
        self.pieces = pieces
        self.usage = usage
        self.delay = delay
        self.calls: list[object] = []

    async def astream(self, messages):
        self.calls.append(messages)
        for index, piece in enumerate(self.pieces):
            if self.delay:
                await asyncio.sleep(self.delay)
            usage = self.usage if index == len(self.pieces) - 1 else None
            yield SimpleNamespace(content=piece, usage_metadata=usage)


class _ExplodingModel:
    async def astream(self, messages):
        raise RuntimeError("provider down")
        yield  # pragma: no cover


def _install_model(monkeypatch, model) -> _FakeChunkModel | _ExplodingModel:
    import yuxi.agents.models as models_module

    monkeypatch.setattr(models_module, "load_chat_model", lambda spec, **kwargs: model)
    return model


def test_sanitize_keeps_valid_questions_in_order():
    raw = ["  灌浆期何时开始？ ", 42, "胚乳细胞如何分化？", None, "淀粉如何合成？"]

    assert _sanitize_followup_questions(raw, "原始问题？") == [
        "灌浆期何时开始？",
        "胚乳细胞如何分化？",
        "淀粉如何合成？",
    ]


def test_sanitize_drops_too_long_duplicate_and_original():
    raw = [
        LONG_QUESTION,
        "灌浆期何时开始？",
        "灌浆期何时开始？",
        "原始问题？",
    ]

    assert _sanitize_followup_questions(raw, "原始问题？") == ["灌浆期何时开始？"]


def test_sanitize_truncates_to_configured_count_and_rejects_non_list():
    raw = [f"问题{i}？" for i in range(8)]

    sanitized = _sanitize_followup_questions(raw, "原始问题？")

    assert len(sanitized) == _FOLLOWUP_QUESTION_COUNT
    assert _sanitize_followup_questions("不是列表", "原始问题？") == []
    assert _sanitize_followup_questions(None, "原始问题？") == []


def test_generate_returns_questions_and_usage(monkeypatch):
    model = _install_model(
        monkeypatch,
        _FakeChunkModel(
            ['```json\n{"questions": ["灌浆期何时开始？", "胚乳细胞如何分化？"]}\n```'],
            usage={"input_tokens": 120, "output_tokens": 30, "total_tokens": 150},
        ),
    )

    result = asyncio.run(
        _generate_followup_suggestions("水稻胚乳发育阶段？", "胚乳发育分三个阶段……", model_spec="openai/gpt-4o-mini")
    )

    assert result is not None
    questions, usage = result
    assert questions == ["灌浆期何时开始？", "胚乳细胞如何分化？"]
    assert usage == {"input_tokens": 120, "output_tokens": 30, "total_tokens": 150}
    # 提示词必须把材料声明为不可信数据（防 RAG 注入），并带上原始问答
    system_prompt = str(model.calls[0][0].content)
    assert "不可信数据" in system_prompt
    assert "水稻胚乳发育阶段？" in str(model.calls[0][1].content)


def test_generate_skips_without_answer_text(monkeypatch):
    model = _install_model(monkeypatch, _FakeChunkModel(["任意输出"]))

    assert asyncio.run(_generate_followup_suggestions("问题？", "   ")) is None
    assert model.calls == []


def test_generate_degrades_to_none_on_model_error_or_bad_payload(monkeypatch):
    _install_model(monkeypatch, _ExplodingModel())
    assert asyncio.run(_generate_followup_suggestions("问题？", "回答")) is None

    _install_model(monkeypatch, _FakeChunkModel(["这不是 JSON"]))
    assert asyncio.run(_generate_followup_suggestions("问题？", "回答")) is None

    # 全部条目被出口校验淘汰（超长）——无有效产出同样降级
    _install_model(monkeypatch, _FakeChunkModel([f'{{"questions": ["{LONG_QUESTION}"]}}']))
    assert asyncio.run(_generate_followup_suggestions("问题？", "回答")) is None


def test_generate_degrades_to_none_on_wall_budget(monkeypatch):
    _install_model(monkeypatch, _FakeChunkModel(["块1", "块2"], delay=0.05))

    assert asyncio.run(_generate_followup_suggestions("问题？", "回答", wall_budget=0.01)) is None
