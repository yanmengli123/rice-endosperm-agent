"""编译器门禁测试（P2，ADR-0010 不变量 L0-3 的执行点）。

钉死：编译产物是 literature_notes 派生产品（入口即断言）；渲染器保留 [Pxxx]
段落锚点（可回跳）；编译链路**零证据面写入**（不触碰 EvidenceEnvelope /
evidence 构造器——由 monkeypatch 计数证明）；L1/L2/L3 渲染产物契约。
"""

from __future__ import annotations

import pytest

from yuxi.literature.boundaries import (
    BoundaryViolationError,
    assert_not_evidence_capable,
)
from yuxi.literature.notes import compiler, prompts, renderers

pytestmark = pytest.mark.unit

_META = {"title": "Starch synthesis in rice", "doi": "10.1002/adma.202407106", "year": "2025"}
_L1L2_JSON = (
    '{"one_liner": "解析 OsNF-YB1 对淀粉合成的调控 [P001]",'
    ' "background": {"text": "水稻淀粉合成背景 [P001]", "paras": ["P001"]},'
    ' "method": {"text": "CRISPR 敲除与酶活测定 [P002]", "paras": ["P002"]},'
    ' "result": {"text": "籽粒淀粉提升 12% [P003]", "paras": ["P003"]},'
    ' "conclusion": {"text": "OsNF-YB1 是正调因子 [P003]", "paras": ["P003"]},'
    ' "innovation": {"text": "首次在胚乳细胞层面定位 [P002]", "paras": ["P002"]},'
    ' "limitation": {"text": "仅单一品种 [P003]", "paras": ["P003"]},'
    ' "wiki": "## 方法论批判\\n对照设置单一（内容）\\n## 可复现性分析\\n参数基本给全（内容）'
    '\\n## 潜在应用与转化路径\\n育种标记辅助（内容，超过五十字符的 wiki 正文段）",'
    ' "concepts": [{"name": "starch biosynthesis", "definition": "淀粉生物合成过程"}],'
    ' "tags": ["rice", "starch"], "ai_value": 4, "topic_score": 0.8}'
)
_PARAGRAPHS = ["背景段落全文。", "方法段落全文。", "结果段落全文。"]


class _FakeResponse:
    def __init__(self, content: str):
        self.content = content


class _FakeAdapter:
    def __init__(self, content: str):
        self._content = content
        self.calls: list[list[dict]] = []

    async def call(self, messages, stream=False):
        assert stream is False
        self.calls.append(messages)
        return _FakeResponse(self._content)


@pytest.mark.asyncio
async def test_compile_l1_l2_uses_model_chain_and_splits_output(monkeypatch):
    adapter = _FakeAdapter(_L1L2_JSON)

    async def _fake_governed_call(messages):
        return await adapter.call(messages, stream=False)

    # 编译器只消费统一治理网关；模型选择/BYOK/计量由网关负责。
    import yuxi.literature.llm_gateway as gateway

    monkeypatch.setattr(gateway, "call", _fake_governed_call)
    result = await compiler.compile_l1_l2(_META, _PARAGRAPHS)

    assert adapter.calls, "必须经文献 LLM 治理网关（不引 openai 直连）"
    # system+user 两消息（前缀缓存形状）
    messages = adapter.calls[0]
    assert messages[0]["role"] == "system"
    assert "[P001] 背景段落全文。" in messages[0]["content"]
    assert messages[1]["role"] == "user"
    assert result["l1"]["one_liner"].startswith("解析 OsNF-YB1")
    assert result["l2"] is not None and "方法论批判" in result["l2"]["wiki"]
    assert result["ai_value"] == 4 and result["topic_score"] == 0.8


@pytest.mark.asyncio
async def test_compile_l1_fails_without_one_liner(monkeypatch):
    async def _no_oneliner(system, user):
        return '{"background": {"text": "缺 one_liner"}}'

    monkeypatch.setattr(compiler, "_complete", _no_oneliner)
    with pytest.raises(ValueError, match="one_liner"):
        await compiler.compile_l1_l2(_META, _PARAGRAPHS)


def test_render_note_preserves_paragraph_anchors():
    data = prompts.parse_compile_json(_L1L2_JSON)
    note = renderers.render_note(_META, data)
    assert "[P001]" in note and "[P003]" in note, "六维渲染必须保留段落 ID（可回跳）"
    assert "starch biosynthesis" in note and "淀粉生物合成过程" in note
    assert note.startswith("---\ntype: paper-note\n")


def test_render_relations_uses_dirname_wikilinks():
    data = {
        "summary": "研究簇概述",
        "concept_map": [{"concept": "淀粉合成", "papers": ["10.1016/j.snb.2022.132616"], "evolution": "演进"}],
        "methodology_connections": [{"from": "10.1/from", "to": "10.2/to", "relation": "方法继承"}],
        "research_trajectory": "趋势",
    }
    related = [{"doi": "10.1016/j.snb.2022.132616", "connection": "方法继承"}]
    md = renderers.render_relations(_META, data, related)
    # DOI 的 "/" → "_"（Obsidian 活链，上游教训：裸 DOI 是死链）
    assert "[[10.1016_j.snb.2022.132616/_note]]" in md
    assert "## 研究簇概述" in md and "→ [[10.2_to/_note]]" in md


def test_literature_notes_stays_derived_and_compile_chain_never_touches_evidence(monkeypatch):
    """L0-3 执行点：literature_notes 是派生产品；编译模块零证据面引用。"""
    assert_not_evidence_capable("literature_notes")  # 派生身份成立
    with pytest.raises(BoundaryViolationError):
        assert_not_evidence_capable("milvus")
    # 编译模块零 yuxi.knowledge 依赖（证据构造面/存储适配器均在知识域；
    # 文档字符串里提及证据词汇不算——ast import 扫描不受注释影响）
    import inspect

    from yuxi.literature.boundaries import find_forbidden_imports
    from yuxi.literature.notes import compiler as compiler_mod

    source = inspect.getsource(compiler_mod)
    assert find_forbidden_imports(source, ("yuxi.knowledge",)) == []


@pytest.mark.asyncio
async def test_complete_json_reasks_once_with_error_context(monkeypatch):
    """解析失败→带错误上下文重问恰好一次；两次都坏才抛错（2026-10-04 真实事故）。

    畸形样例刻意用"串内未转义引号"——确定性修复级联明确不覆盖的形态。
    """
    calls: list[str] = []

    async def _flaky_complete(system: str, user: str) -> str:
        calls.append(user)
        if len(calls) == 1:
            # one_liner 值内嵌裸双引号：json.loads 必坏且修复级联不救
            return '{"one_liner": "RISBZ1 是"刹车"因子", "ai_value": 2.5}'
        return '{"one_liner": "RISBZ1 是刹车因子", "ai_value": 2.5}'

    monkeypatch.setattr(compiler, "_complete", _flaky_complete)
    data, raw = await compiler.complete_json("sys", "原始任务")
    assert data["one_liner"] == "RISBZ1 是刹车因子"
    assert len(calls) == 2, "重问恰好一次"
    assert "解析错误" in calls[1] and "原始任务" in calls[1], "重问必须携带原任务与错误上下文"

    calls.clear()

    async def _always_bad(system: str, user: str) -> str:
        calls.append(user)
        return "完全不是 JSON"

    monkeypatch.setattr(compiler, "_complete", _always_bad)
    with pytest.raises(ValueError):
        await compiler.complete_json("sys", "task")
    assert len(calls) == 2, "重问上限一次，不无限循环"
