"""P0 Authority Gate 与产品注册中心契约测试。

覆盖四平面不变量的机器可验证部分：
- WikiNavigationHit ∉ EvidenceEnvelope
- kb_type=llmwiki 不能注册/创建为 KnowledgeBase 存储适配器
- scope gateway 对派生产品关闭全部证据通道
- answer context 构建前丢弃派生产品检索行
"""

from __future__ import annotations

import pytest

from yuxi.knowledge.products.authority_gate import AuthorityGate, AuthorityGateError, gate_evidence
from yuxi.knowledge.products.contracts import EvidenceEnvelope, WikiNavigationHit, to_evidence_envelope
from yuxi.knowledge.products.registry import (
    get_product_spec,
    registry_snapshot,
    require_capability,
)


# --- 注册中心 -----------------------------------------------------------------


def test_llmwiki_is_declared_as_navigation_only_derived_product():
    spec = get_product_spec("llmwiki")
    assert spec.category == "derived_product"
    assert spec.trust_class == "DERIVED"
    assert spec.authority_class == "NAVIGATION_ONLY"
    assert spec.capabilities.supports_upload is False
    assert spec.capabilities.supports_raw_evidence is False
    assert spec.capabilities.supports_navigation is True
    assert spec.capabilities.supports_publication is True
    assert spec.capabilities.supports_rollback is True


def test_unknown_kb_types_fail_closed_until_registered():
    spec = get_product_spec("milvus-plus")
    assert spec.category == "unregistered"
    assert spec.capabilities.supports_upload is False
    assert spec.capabilities.supports_raw_evidence is False


def test_registry_snapshot_exposes_both_categories():
    snapshot = {item["kb_type"]: item for item in registry_snapshot()}
    assert snapshot["llmwiki"]["category"] == "derived_product"
    assert snapshot["milvus"]["category"] == "authority_source"
    # ADR-0010：文献域 L1/L2/L3 笔记是第二个派生产品类型，快照必须暴露其三值
    assert snapshot["literature_notes"]["category"] == "derived_product"
    assert snapshot["literature_notes"]["trust_class"] == "DERIVED"
    assert snapshot["literature_notes"]["authority_class"] == "NAVIGATION_ONLY"
    # 快照锁面：登记条目集合即产品目录，增删都必须是有意为之
    assert set(snapshot) == {"milvus", "dify", "notion", "llmwiki", "literature_notes"}


def test_require_capability_rejects_upload_for_llmwiki():
    with pytest.raises(TypeError, match="does not support capability 'supports_upload'"):
        require_capability("llmwiki", "supports_upload")
    # 权威源不受影响
    require_capability("milvus", "supports_upload")


# --- 文献域产品（ADR-0010：L1/L2/L3 是导航平面派生物） --------------------------


def test_literature_notes_is_declared_as_navigation_only_derived_product():
    spec = get_product_spec("literature_notes")
    assert spec.category == "derived_product"
    assert spec.trust_class == "DERIVED"
    assert spec.authority_class == "NAVIGATION_ONLY"
    assert spec.capabilities.supports_upload is False
    assert spec.capabilities.supports_raw_evidence is False
    # 与 llmwiki 同能力面：可导航/可发布/可回滚（经 Wiki 发布面生命周期管理）
    assert spec.capabilities.supports_navigation is True
    assert spec.capabilities.supports_publication is True
    assert spec.capabilities.supports_rollback is True


def test_to_evidence_envelope_rejects_literature_notes_rows():
    # 即使把 L1/L2/L3 笔记正文伪装成检索行，类型边界也拒绝其进入证据通道
    with pytest.raises(AuthorityGateError, match="cannot enter the evidence channel"):
        to_evidence_envelope(
            {"evidence_id": "ev_note", "content": "L1 一句话贡献 + 六维笔记", "kb_type": "literature_notes"}
        )


def test_require_capability_rejects_upload_for_literature_notes():
    with pytest.raises(TypeError, match="does not support capability 'supports_upload'"):
        require_capability("literature_notes", "supports_upload")
    with pytest.raises(TypeError, match="does not support capability 'supports_raw_evidence'"):
        require_capability("literature_notes", "supports_raw_evidence")
    # 权威源不受影响
    require_capability("milvus", "supports_upload")


def test_empty_kb_type_rows_currently_pass_as_authority_known_limitation():
    """ADR-0010 已知限制 §4：空 kb_type 行沿用「冻结时已校验」的历史假设放行。

    本测试**钉住现状**（registry.get_product_spec("") 与 answer_context_builder
    两处成对 fail-open），不是认可该行为。收紧边界的前置条件：P1 存量审计
    （扫描冻结 scope 快照与证据行构造点，统计真实缺 kb_type 的行数），且必须
    走独立 PR——直接收紧会让 filter_evidence_rows 静默丢弃存量证据行，表现为
    回答悄悄变差。yuxi/literature 自身构造的行必填 kb_type（由文献域测试保证）。
    """
    spec = get_product_spec("")
    assert spec.category == "authority_source"  # 现状：fail-open（历史行兼容）
    envelope = to_evidence_envelope({"evidence_id": "ev_legacy", "content": "row without kb_type"})
    assert isinstance(envelope, EvidenceEnvelope)


# --- 类型契约 -----------------------------------------------------------------


def test_wiki_navigation_hit_cannot_enter_evidence_channel():
    hit = WikiNavigationHit(
        wiki_id="wiki_1",
        publication_id="pub_1",
        channel="ENTITY",
        entity_id="e1",
        entity_name="OsNF-YB1",
    )
    with pytest.raises(AuthorityGateError):
        AuthorityGate.reject_navigation_as_evidence([{"evidence_id": "x", "content": "ok"}, hit])


def test_wiki_navigation_hit_has_no_text_fields():
    hit = WikiNavigationHit(wiki_id="w", publication_id="p", channel="ALIAS")
    payload = hit.to_dict()
    for forbidden in ("content", "text", "quote", "evidence_id"):
        assert forbidden not in payload


def test_to_evidence_envelope_rejects_derived_product_rows():
    with pytest.raises(AuthorityGateError, match="cannot enter the evidence channel"):
        to_evidence_envelope({"evidence_id": "e1", "content": "wiki page body", "kb_type": "llmwiki"})


def test_to_evidence_envelope_accepts_authority_rows():
    envelope = to_evidence_envelope(
        {
            "evidence_id": "ev_1",
            "content": "OsNF-YB1 regulates starch synthesis.",
            "kb_id": "kb_1",
            "kb_type": "milvus",
            "source_type": "DOCUMENT",
            "raw_score": 0.9,
        }
    )
    assert isinstance(envelope, EvidenceEnvelope)
    assert envelope.origin == "DOCUMENT"
    assert envelope.claim_eligible is False


def test_to_evidence_envelope_rejects_unregistered_product_rows():
    with pytest.raises(AuthorityGateError, match="not registered as an evidence authority"):
        to_evidence_envelope(
            {
                "evidence_id": "ev_unknown",
                "content": "unregistered connector result",
                "kb_type": "custom-unknown",
            }
        )


def test_gate_evidence_drops_derived_rows_and_keeps_authority_rows():
    rows = [
        {"evidence_id": "ev_ok", "content": "authoritative", "kb_type": "milvus"},
        {"evidence_id": "ev_wiki", "content": "derived page", "kb_type": "llmwiki"},
        "not-a-dict",
        {"content": "missing id"},
    ]
    envelopes = gate_evidence(rows)
    assert [item.evidence_id for item in envelopes] == ["ev_ok"]


# --- 工厂隔离 -----------------------------------------------------------------


def test_factory_refuses_to_register_or_create_derived_products():
    from yuxi.knowledge.base import KBNotFoundError, KnowledgeBase
    from yuxi.knowledge.factory import KnowledgeBaseFactory

    class FakeWikiKB(KnowledgeBase):
        kb_type = "llmwiki"
        name = "wiki"
        description = "derived"

        def __init__(self, work_dir):
            super().__init__(work_dir)

    try:
        with pytest.raises(ValueError, match="derived product"):
            KnowledgeBaseFactory.register(FakeWikiKB)
        assert KnowledgeBaseFactory.is_type_supported("llmwiki") is False
        with pytest.raises(KBNotFoundError, match="derived knowledge product"):
            KnowledgeBaseFactory.create("llmwiki", "/tmp/nowhere")
        with pytest.raises(KBNotFoundError, match="derived knowledge product"):
            KnowledgeBaseFactory.get_kb_class("llmwiki")
        registry = {item["kb_type"]: item for item in KnowledgeBaseFactory.get_product_registry()}
        assert registry["llmwiki"]["capabilities"]["supports_upload"] is False
    finally:
        KnowledgeBaseFactory._kb_types.pop("llmwiki", None)


# --- 检索网关隔离 --------------------------------------------------------------


def test_scope_gateway_skips_derived_members_in_evidence_fanout():
    import asyncio

    import yuxi.knowledge.scope_gateway as gateway

    derived_member = {"kb_id": "kb_wiki", "kb_type": "llmwiki", "document_enabled": True}
    authority_member = {"kb_id": "kb_doc", "kb_type": "milvus", "document_enabled": True}

    assert gateway._member_is_derived(derived_member) is True
    assert gateway._member_is_derived(authority_member) is False

    rows, error = asyncio.run(gateway._query_document_source(derived_member, "OsNF-YB1"))
    assert rows == []
    assert error == "DERIVED_PRODUCT_CHANNEL_DENIED"

    rows, error = asyncio.run(gateway._query_managed_graph_source(derived_member, "OsNF-YB1", limit=8))
    assert rows == []
    assert error == "DERIVED_PRODUCT_CHANNEL_DENIED"


# --- 答案上下文门禁 ------------------------------------------------------------


def test_scope_gateway_skips_literature_notes_members_in_evidence_fanout():
    import asyncio

    import yuxi.knowledge.scope_gateway as gateway

    # ADR-0010 决议 4：登记 literature_notes 后，scope_gateway 三通道封锁自动生效
    derived_member = {"kb_id": "kb_lit", "kb_type": "literature_notes", "document_enabled": True}
    authority_member = {"kb_id": "kb_doc", "kb_type": "milvus", "document_enabled": True}

    assert gateway._member_is_derived(derived_member) is True
    assert gateway._member_is_derived(authority_member) is False

    rows, error = asyncio.run(gateway._query_document_source(derived_member, "OsNF-YB1"))
    assert rows == []
    assert error == "DERIVED_PRODUCT_CHANNEL_DENIED"

    rows, error = asyncio.run(gateway._query_managed_graph_source(derived_member, "OsNF-YB1", limit=8))
    assert rows == []
    assert error == "DERIVED_PRODUCT_CHANNEL_DENIED"


def test_answer_context_drops_derived_rows_and_reports_gate():
    from yuxi.knowledge.rendering.answer_context_builder import build_answer_context

    contract = {
        "knowledge_scope_snapshot": {"scope_id": "s1"},
        "claims": [],
        "evidence": [
            {
                "evidence_id": "ev_1",
                "kb_type": "milvus",
                "evidence_quote": "quote",
                "subject": {"name": "OsNF-YB1"},
                "predicate": "REGULATES",
                "object": {"name": "starch"},
            },
            {"evidence_id": "ev_2", "kb_type": "llmwiki", "evidence_quote": "wiki body"},
        ],
        "retrieval_plan": {"intent": "GENERAL_KNOWLEDGE_QUERY"},
        "completeness": {},
    }
    context = build_answer_context(contract)
    assert "wiki body" not in context  # 派生行被 Authority Gate 丢弃（v3 起丢弃计数不再进 LLM payload）


def test_answer_context_rejects_navigation_hit_in_evidence():
    from yuxi.knowledge.rendering.answer_context_builder import build_answer_context

    contract = {
        "evidence": [
            WikiNavigationHit(wiki_id="w", publication_id="p", channel="ENTITY"),
        ],
    }
    with pytest.raises(AuthorityGateError):
        build_answer_context(contract)
