"""citation_ready v2 契约（图卡 P0-2 / Commit B）。

覆盖：载荷助手的开关语义（字段缺席 ⟺ 未发布）、kb_id/revision_id 硬前置、
复合路径末 AI 消息落 citation_binding（历史恢复可读回 figure_projection）、
Config 开关默认关闭。
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest
import yuxi.services.chat_service as svc
from langchain_core.messages import AIMessage, HumanMessage
from yuxi.config.app import Config

pytestmark = [pytest.mark.unit]

_FIGURE = {
    "projection_version": "figure_projection_v1",
    "binding_id": "vlb_" + "1" * 20,
    "kb_id": "kb-a",
    "file_id": "file_1",
    "revision_id": "pr_1",
    "asset_name": "0123456789abcdef012345678-fig1.png",
    "asset_sha256": "b" * 64,
    "media_type": "image/png",
    "page": 3,
    "panel_match": None,
    "evidence_id": "ev_1",
    "figure_label": "Figure 1",
    "caption": "Figure 1. Expression patterns.",
    "width": 800,
    "height": 600,
    "selection": {"asset_count": 1, "rule": "primary_first_then_reading_order"},
}

_LOCATOR = {
    "status": "VERIFIED",
    "locator_kind": "FIGURE_CAPTION",
    "evidence_id": "ev_1",
    "file_id": "file_1",
    "filename": "paper.pdf",
    "zone": "MAIN_TEXT",
    "page": 3,
    "anchor_id": "ea_1",
    "kb_id": "kb-a",
    "parse_revision_id": "pr_1",
    "figure_projection": {
        "version": "figure_projection_v1",
        "status": "attached",
        "reason": None,
        "figures": [_FIGURE],
    },
}


def test_config_switch_defaults_off():
    assert Config.model_fields["figure_card_enabled"].default is False
    assert Config.model_fields["graph_card_enabled"].default is False


def test_ordinary_figure_answer_removes_ungrounded_mechanism_claim(monkeypatch):
    from yuxi.knowledge.rendering.claim_evidence_resolver import normalize_for_match

    caption = "Figure 2 CRISPR/Cas9 mutant grain phenotype and grain length evaluation."
    citation = {
        "ref": "E1",
        "evidence_id": "ev_1",
        "kb_id": "kb-a",
        "file_id": "file_1",
        "filename": "paper.pdf",
        "zone": "MAIN_TEXT",
        "page_numbers": [5],
        "primary_page": 5,
        "quote_head": caption,
        "anchor_ids": ["ea_1"],
        "locatable": True,
        "toc_line": False,
        "secondary_of": None,
        "_quote": caption,
        "_quote_norm": normalize_for_match(caption),
        "_anchor_id": "ea_1",
        "_parse_revision_id": "pr_1",
        "_evidence_type": "caption",
    }
    contract = {
        "status": "COMPLETED",
        "citations": [citation],
        "locator_resolution": {},
    }
    monkeypatch.setattr(svc.conf, "figure_ref_anchor_enabled", True)
    monkeypatch.setattr(svc.conf, "figure_semantic_gate_enabled", False)

    guarded, validation = svc._guard_knowledge_answer(
        "Figure 2 展示 CRISPR 突变体籽粒表型。OsMYB73 调控生长素生物合成通路。",
        contract,
    )

    assert "〔图表F1｜Figure 2〕" in guarded
    assert "调控生长素生物合成通路" not in guarded
    assert validation["mechanism_claims_removed"] == 1


def test_graph_snapshot_publish_switch_is_fail_closed(monkeypatch):
    snapshot = {
        "schema": "graph_snapshot_v1",
        "outcome": "HIT",
        "nodes": [{"entity_id": "e1", "kb_id": "kb", "name": "GS3", "label": "Gene", "is_seed": True}],
        "edges": [
            {
                "triple_id": "t1",
                "kb_id": "kb",
                "source_entity_id": "e1",
                "target_entity_id": "e2",
                "predicate": "regulates",
                "review_status": "APPROVED",
                "conflict_status": "NONE",
            }
        ],
    }
    contract = {"graph_snapshot": snapshot}
    monkeypatch.setattr(svc.conf, "graph_card_enabled", False)
    assert svc._published_graph_snapshot(contract) is None
    monkeypatch.setattr(svc.conf, "graph_card_enabled", True)
    assert svc._published_graph_snapshot(contract) == snapshot
    # 非 HIT（无边）不附卡：PENDING_REVIEW 等裁决由答案文本承载，不渲染空卡片
    empty = {"graph_snapshot": {**snapshot, "outcome": "PENDING_REVIEW", "edges": [], "nodes": []}}
    assert svc._published_graph_snapshot(empty) is None
    assert svc._published_graph_snapshot({"graph_snapshot": {"schema": "unknown"}}) is None


def test_relation_answer_uses_only_frozen_snapshot():
    contract = {
        "retrieval_plan": {"intent": "RELATION_LOOKUP"},
        "graph_snapshot": {
            "schema": "graph_snapshot_v1",
            "outcome": "HIT",
            "truncated": False,
            "nodes": [
                {"entity_id": "e1", "name": "GS3"},
                {"entity_id": "e2", "name": "grain size"},
            ],
            "edges": [
                {
                    "source_entity_id": "e1",
                    "target_entity_id": "e2",
                    "predicate": "regulates",
                    "conflict_status": "NONE",
                    "review_status": "APPROVED",
                }
            ],
            "suppressed": {"review_policy": 0},
        },
    }
    assert svc._deterministic_relation_answer(contract) == (
        "已从规范知识图谱检索到以下已审核关系：\n- GS3 —regulates→ grain size"
    )


def test_relation_answer_discloses_candidates_in_mixed_group():
    contract = {
        "retrieval_plan": {"intent": "RELATION_LOOKUP"},
        "graph_snapshot": {
            "schema": "graph_snapshot_v1",
            "outcome": "HIT",
            "nodes": [
                {"entity_id": "e1", "name": "GS3"},
                {"entity_id": "e2", "name": "grain size"},
            ],
            "edges": [
                {
                    "triple_id": "reviewed",
                    "source_entity_id": "e1",
                    "target_entity_id": "e2",
                    "predicate": "regulates",
                    "review_status": "APPROVED",
                    "parallel_count": 2,
                    "candidate_parallel_count": 1,
                    "conflict_status": "NONE",
                }
            ],
            "suppressed": {"review_policy": 0},
        },
    }

    assert svc._deterministic_relation_answer(contract) == (
        "已从规范知识图谱检索到以下关系（含标注待审核的候选关系）：\n"
        "- GS3 —regulates→ grain size（另含 1 条待审核平行断言）（2 条平行抽取记录）"
    )


def test_relation_answer_reports_pending_review_with_count():
    contract = {
        "retrieval_plan": {"intent": "RELATION_LOOKUP"},
        "graph_snapshot": {
            "schema": "graph_snapshot_v1",
            "outcome": "PENDING_REVIEW",
            "seed_names": ["OsMYB73"],
            "nodes": [],
            "edges": [],
            "suppressed": {"review_policy": 729},
        },
    }
    assert svc._deterministic_relation_answer(contract) == (
        "在当前知识范围内找到 729 条与「OsMYB73」相关的候选关系，"
        "均来自自动抽取、尚待人工审核，暂不在关系图中展示。"
        "可在图谱审核工作台完成审核后查看，或由管理员为该知识库开启候选证据策略。"
    )


def test_payload_adds_kb_and_revision_and_omits_figures_when_switch_off(monkeypatch):
    monkeypatch.setattr(svc.conf, "figure_card_enabled", False)
    payload = svc._citation_ready_payload(_LOCATOR)
    citation = payload["citation"]
    assert citation["kb_id"] == "kb-a"
    assert citation["revision_id"] == "pr_1"
    assert citation["page"] == 3 and citation["evidence_id"] == "ev_1" and citation["filename"] == "paper.pdf"
    assert "figures" not in payload  # 开关关：投影仍在 locator 里，但 SSE 不发布


def test_payload_carries_figures_only_when_switch_on_and_attached(monkeypatch):
    monkeypatch.setattr(svc.conf, "figure_card_enabled", True)
    payload = svc._citation_ready_payload(_LOCATOR)
    assert payload["figures"] == [_FIGURE]

    suppressed = {
        **_LOCATOR,
        "figure_projection": {
            "version": "figure_projection_v1",
            "status": "suppressed",
            "reason": "no_asset_row",
            "figures": [],
        },
    }
    assert "figures" not in svc._citation_ready_payload(suppressed)
    # 旧 run / 无投影信封：同样缺席，不抛
    assert "figures" not in svc._citation_ready_payload({k: v for k, v in _LOCATOR.items() if k != "figure_projection"})


async def _save_compound_turn(
    contract: dict,
    *,
    final_content_override: str | None = None,
    final_additional_kwargs: dict | None = None,
) -> tuple[dict, int]:
    """复合路径经 save_messages_from_langgraph_state 落库，返回末 AI 消息 extra_metadata 与 flush 次数。"""

    class FakeDB:
        def __init__(self):
            self.flushes = 0

        async def flush(self):
            self.flushes += 1

    class FakeRepo:
        def __init__(self, db):
            self.db = db
            self.messages: list[SimpleNamespace] = []

        async def add_message_by_thread_id(self, **kwargs):
            message = SimpleNamespace(
                id=len(self.messages) + 1,
                content=kwargs.get("content"),
                extra_metadata=dict(kwargs.get("extra_metadata") or {}),
            )
            self.messages.append(message)
            return message

        async def get_messages_by_thread_id(self, _thread_id):
            return []

        async def add_tool_call(self, **kwargs):
            return SimpleNamespace(id=1)

    class FakeGraph:
        async def aget_state(self, _config):
            return SimpleNamespace(
                values={
                    "messages": [
                        HumanMessage(content="Figure 1 是什么意思"),
                        # already_guarded：守卫已在流末执行，这里不重复跑 citation channel
                        AIMessage(
                            id="ai-1",
                            content="已可靠定位到原文：〔引文定位｜MAIN_TEXT·第3页｜paper.pdf〕\n解释……",
                            additional_kwargs={"locator_validation": {"status": "RENDERED_V2"}},
                        ),
                    ]
                }
            )

    class FakeAgent:
        async def get_graph(self, *, context):
            return FakeGraph()

    db = FakeDB()
    repo = FakeRepo(db)
    await svc.save_messages_from_langgraph_state(
        agent_instance=FakeAgent(),
        thread_id="thread-1",
        conv_repo=repo,
        config_dict={"configurable": {"thread_id": "thread-1", "uid": "user-1"}},
        context=object(),
        knowledge_contract=contract,
        final_content_override=final_content_override,
        final_additional_kwargs=final_additional_kwargs,
    )
    return repo.messages[-1].extra_metadata, db.flushes


@pytest.mark.asyncio
async def test_compound_path_persists_binding_and_published_payload(monkeypatch):
    """复合路径末 AI 消息必须带与确定性路径同形的 citation_binding（审计）与 citation_ready
    （实际发布载荷，含 figures）——历史恢复只读后者。"""
    monkeypatch.setattr(svc.conf, "figure_card_enabled", True)
    contract = {"status": "COMPLETED", "retrieval_id": "kr_1", "citations": [], "locator_resolution": _LOCATOR}

    saved, flushes = await _save_compound_turn(contract)

    assert saved["knowledge_retrieval_id"] == "kr_1"
    assert saved["citation_binding"] == _LOCATOR
    assert saved["citation_ready"]["citation"]["kb_id"] == "kb-a"
    assert saved["citation_ready"]["citation"]["revision_id"] == "pr_1"
    assert saved["citation_ready"]["figures"] == [_FIGURE]
    assert flushes >= 1


@pytest.mark.asyncio
async def test_compound_path_persists_final_semantic_validation_not_state_draft():
    contract = {"status": "COMPLETED", "retrieval_id": "kr_1", "citations": [], "locator_resolution": _LOCATOR}
    final_validation = {
        "status": "DEGRADED",
        "table_claim_validation": {"status": "DEGRADED", "facts_available": 39},
    }
    saved, _ = await _save_compound_turn(
        contract,
        final_content_override="最终安全正文",
        final_additional_kwargs={"locator_validation": final_validation},
    )
    assert saved["additional_kwargs"]["locator_validation"] == final_validation


def test_table_ranking_gate_reads_question_from_retrieval_summary():
    table = {
        "label": "Table 1",
        "header_rows": 1,
        "rows": [
            [
                {"text": "Genotype"},
                {"text": "Chalkiness (%)"},
            ],
            [{"text": "WT (ZH11)"}, {"text": "12.1"}],
            [{"text": "osmyb73"}, {"text": "26.8"}],
        ],
    }
    guarded, validation = svc._guard_knowledge_answer(
        "osmyb73 热胁迫后垩白度为 26.8%，受影响最大。",
        {
            "status": "COMPLETED",
            "citations": [],
            "retrieval_summary": {"query": "哪个基因型的垩白度受热胁迫影响最大？"},
        },
        semantic_tables=[table],
        table_semantic_ready=True,
    )
    assert "无法进行该比较" in guarded
    assert "26.8" not in guarded
    assert validation["table_claim_validation"]["status"] == "REFUSED_INCOMPLETE_OPERANDS"


@pytest.mark.asyncio
async def test_compound_path_history_payload_omits_figures_when_switch_off(monkeypatch):
    """暗发布期：投影仍在 citation_binding 供审计，但 citation_ready 无 figures——刷新不漏图卡。"""
    monkeypatch.setattr(svc.conf, "figure_card_enabled", False)
    contract = {"status": "COMPLETED", "retrieval_id": "kr_1", "citations": [], "locator_resolution": _LOCATOR}

    saved, _ = await _save_compound_turn(contract)

    assert saved["citation_binding"]["figure_projection"]["status"] == "attached"
    assert "figures" not in saved["citation_ready"]
    assert saved["citation_ready"]["citation"]["page"] == 3


@pytest.mark.asyncio
async def test_unverified_locator_persists_binding_without_citation_ready():
    unverified = {
        **_LOCATOR,
        "status": "NOT_FOUND",
        "figure_projection": {"status": "suppressed", "reason": "binding_not_verified", "figures": []},
    }
    contract = {"status": "DEGRADED", "retrieval_id": "kr_2", "citations": [], "locator_resolution": unverified}

    saved, _ = await _save_compound_turn(contract)

    assert saved["citation_binding"] == unverified
    assert "citation_ready" not in saved


@pytest.mark.asyncio
async def test_skipped_contract_does_not_persist_citation_binding():
    class FakeRepo:
        def __init__(self):
            self.db = SimpleNamespace()
            self.messages: list[SimpleNamespace] = []

        async def add_message_by_thread_id(self, **kwargs):
            message = SimpleNamespace(id=1, extra_metadata=dict(kwargs.get("extra_metadata") or {}))
            self.messages.append(message)
            return message

        async def get_messages_by_thread_id(self, _thread_id):
            return []

        async def add_tool_call(self, **kwargs):
            return SimpleNamespace(id=1)

    class FakeGraph:
        async def aget_state(self, _config):
            return SimpleNamespace(values={"messages": [AIMessage(id="ai-1", content="闲聊回答")]})

    class FakeAgent:
        async def get_graph(self, *, context):
            return FakeGraph()

    repo = FakeRepo()
    await svc.save_messages_from_langgraph_state(
        agent_instance=FakeAgent(),
        thread_id="thread-1",
        conv_repo=repo,
        config_dict={"configurable": {"thread_id": "thread-1", "uid": "user-1"}},
        context=object(),
        knowledge_contract={"status": "SKIPPED"},
    )
    assert "citation_binding" not in repo.messages[-1].extra_metadata
