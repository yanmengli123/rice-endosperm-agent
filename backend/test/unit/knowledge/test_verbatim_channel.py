"""VERBATIM（GREP）通道单测：L1/L2/L3 检索、安全边界、gateway 集成与护栏。

护栏（企业级验收的四条硬约束）：
1. 跨租户 pattern 不泄漏（SQL 谓词强制 tenant_id）；
2. 已淘汰 revision 的 span 不命中（active_parse_revision_id 过滤）；
3. 派生产品成员不产生 VERBATIM 通道任务（Authority Gate）；
4. VERBATIM 行 claim_eligible 恒为 False 且必须可回源定位。
"""

from __future__ import annotations

import hashlib

import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from yuxi.knowledge import scope_gateway
from yuxi.knowledge.evidence.verbatim import (
    escape_like,
    extract_verbatim_patterns,
    query_verbatim_evidence,
    sanitize_verbatim_patterns,
    typed_keywords,
)
from yuxi.knowledge.planning.task_classifier import detect_question_types
from yuxi.knowledge.validation.context_evidence_validator import validate_context_evidence
from yuxi.storage.postgres.models_knowledge import (
    EvidenceSpanRecord,
    KnowledgeFile,
    ScientificLexicalIndexRecord,
)

pytestmark = [pytest.mark.unit]


def _digest(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


# ========== 纯函数层 ==========


def test_escape_like_neutralizes_wildcards():
    assert escape_like("50%") == "50/%"
    assert escape_like("Os_1") == "Os/_1"
    assert escape_like("a/b") == "a//b"
    assert escape_like("plain") == "plain"


def test_extract_verbatim_patterns_from_quotes_and_identifier_tokens():
    patterns = extract_verbatim_patterns('论文里"two typical SANT domains"在哪一页？LOC_Os01g01010.1 的表述呢')
    assert "two typical SANT domains" in patterns
    assert "LOC_Os01g01010.1" in patterns


def test_extract_verbatim_patterns_excludes_citation_like_and_enforces_window():
    patterns = extract_verbatim_patterns("doi 10.1111/pbi.14558 与 ab 是短词")
    assert not patterns  # DOI 由 L1 citation 键覆盖；<3 字符/无信号均不产模式


def test_sanitize_verbatim_patterns_clamps_length_and_count():
    assert sanitize_verbatim_patterns(["ab", "x" * 200, "valid pattern", "valid pattern", "another one"]) == [
        "valid pattern",
        "another one",
    ]


def test_typed_keywords_reuses_index_side_folding():
    keywords = typed_keywords("OsMYB73 spans 115-164 aa; PMID: 35084453; see Figure 2A")
    folded = set(keywords)
    assert ("identifier", "osmyb73") in folded
    assert ("numeric", "115:164") in folded
    assert ("citation", "pmid:35084453") in folded
    assert ("figure_table", "2a") in folded


def test_detect_question_types_adds_verbatim_only_for_quote_or_intent():
    assert "VERBATIM" in detect_question_types('请给出"increased grain weight"的原句')
    assert "VERBATIM" in detect_question_types("逐字给出结论原文")
    assert "VERBATIM" not in detect_question_types("LOC_Os01g01010 是什么基因")
    assert "ENTITY" in detect_question_types("LOC_Os01g01010 是什么基因")


# ========== SQLite 集成层（查询谓词） ==========


@pytest_asyncio.fixture
async def span_session():
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as connection:
        await connection.run_sync(KnowledgeFile.__table__.create)
        await connection.run_sync(EvidenceSpanRecord.__table__.create)
        await connection.run_sync(ScientificLexicalIndexRecord.__table__.create)
    session_factory = async_sessionmaker(engine, expire_on_commit=False)
    async with session_factory() as session:
        yield session
    await engine.dispose()


def _seed_span(
    session,
    *,
    row_id: int,
    tenant_id: int = 1,
    kb_id: str = "kb-a",
    file_id: str = "file-a",
    revision: str = "rev-1",
    active_revision: str | None = None,
    span_id: str,
    evidence_id: str,
    quote: str,
    anchor_id: str | None = None,
    page: int | None = 3,
    sentence_index: int = 0,
):
    session.add(
        KnowledgeFile(
            id=row_id,
            file_id=file_id,
            kb_id=kb_id,
            filename=f"{file_id}.pdf",
            active_parse_revision_id=active_revision if active_revision is not None else revision,
        )
    )
    session.add(
        EvidenceSpanRecord(
            id=row_id,
            tenant_id=tenant_id,
            parse_revision_id=revision,
            kb_id=kb_id,
            file_id=file_id,
            span_id=span_id,
            evidence_id=evidence_id,
            anchor_id=anchor_id,
            sentence_index=sentence_index,
            quote=quote,
            quote_hash=_digest(quote),
            page_number=page,
            evidence_type="sentence",
        )
    )


def _seed_lexical(
    session,
    *,
    row_id: int,
    tenant_id: int = 1,
    kb_id: str = "kb-a",
    file_id: str = "file-a",
    revision: str = "rev-1",
    owner_span_id: str,
    lex_type: str,
    value: str,
    folded: str,
):
    session.add(
        ScientificLexicalIndexRecord(
            id=row_id,
            tenant_id=tenant_id,
            parse_revision_id=revision,
            kb_id=kb_id,
            file_id=file_id,
            owner_type="span",
            owner_id=owner_span_id,
            lex_type=lex_type,
            lex_value=value,
            lex_value_folded=folded,
        )
    )


@pytest.mark.asyncio
async def test_l1_typed_key_returns_owner_span_not_revision_neighbor(span_session):
    """回归护栏：旧 lexical_hints 仅按 revision join，会返回同 revision 任意 span。"""
    _seed_span(
        span_session,
        row_id=1,
        span_id="es_owner",
        evidence_id="evs_owner",
        quote="OsMYB73 regulates grain size.",
        anchor_id="ea_1",
    )
    _seed_span(
        span_session,
        row_id=2,
        file_id="file-b",
        span_id="es_other",
        evidence_id="evs_other",
        quote="Unrelated sentence about watering schedule.",
        anchor_id="ea_2",
        sentence_index=1,
    )
    # 注意：两条 span 同属 rev-1；词法键只属于 es_owner
    _seed_lexical(
        span_session, row_id=10, owner_span_id="es_owner", lex_type="identifier", value="OsMYB73", folded="osmyb73"
    )
    await span_session.flush()

    result = await query_verbatim_evidence(span_session, tenant_id=1, kb_ids=["kb-a"], question="OsMYB73", patterns=[])
    assert [span["span_id"] for span in result["spans"]] == ["es_owner"]
    assert result["spans"][0]["match_tier"] == "TYPED_KEY"
    assert result["hit_counts"]["typed_key"] == 1


@pytest.mark.asyncio
async def test_tenant_isolation_is_enforced_in_sql(span_session):
    _seed_span(
        span_session,
        row_id=1,
        tenant_id=1,
        kb_id="kb-a",
        span_id="es_t1",
        evidence_id="evs_t1",
        quote="tenant one owns this OsMYB73 sentence.",
    )
    _seed_span(
        span_session,
        row_id=2,
        tenant_id=2,
        kb_id="kb-a",
        file_id="file-t2",
        span_id="es_t2",
        evidence_id="evs_t2",
        quote="tenant two OsMYB73 sentence must never leak.",
    )
    await span_session.flush()

    result = await query_verbatim_evidence(
        span_session, tenant_id=1, kb_ids=["kb-a"], question="OsMYB73 在哪", patterns=["OsMYB73"]
    )
    assert [span["span_id"] for span in result["spans"]] == ["es_t1"]


@pytest.mark.asyncio
async def test_superseded_revision_spans_are_invisible(span_session):
    _seed_span(
        span_session,
        row_id=1,
        file_id="file-old",
        revision="rev-old",
        active_revision="rev-new",  # 文件已切到新 revision，旧 span 必须不可见
        span_id="es_old",
        evidence_id="evs_old",
        quote="stale OsMYB73 claim was superseded.",
    )
    _seed_span(
        span_session,
        row_id=2,
        file_id="file-new",
        revision="rev-new",
        span_id="es_new",
        evidence_id="evs_new",
        quote="current OsMYB73 claim stands.",
    )
    await span_session.flush()

    result = await query_verbatim_evidence(
        span_session, tenant_id=1, kb_ids=["kb-a"], question="OsMYB73", patterns=["OsMYB73"]
    )
    assert [span["span_id"] for span in result["spans"]] == ["es_new"]


@pytest.mark.asyncio
async def test_l2_substring_matches_quoted_phrase_and_escapes_percent(span_session):
    _seed_span(
        span_session,
        row_id=1,
        span_id="es_pct",
        evidence_id="evs_pct",
        quote="yield reached 50% in the field trial.",
    )
    _seed_span(
        span_session,
        row_id=2,
        file_id="file-b",
        span_id="es_plain",
        evidence_id="evs_plain",
        quote="grain number 50 increased slightly.",
    )
    await span_session.flush()

    # "50%" 必须按字面匹配：含 "50" 但不含 "50%" 的句子不得被 % 通配符误召回
    result = await query_verbatim_evidence(span_session, tenant_id=1, kb_ids=["kb-a"], question="50%", patterns=["50%"])
    assert [span["span_id"] for span in result["spans"]] == ["es_pct"]
    assert result["spans"][0]["match_tier"] == "EXACT_SUBSTRING"

    quoted = await query_verbatim_evidence(
        span_session,
        tenant_id=1,
        kb_ids=["kb-a"],
        question="“yield reached 50%”的原句在哪",
    )
    assert [span["span_id"] for span in quoted["spans"]] == ["es_pct"]


@pytest.mark.asyncio
async def test_l3_token_gap_tolerates_line_breaks(span_session):
    _seed_span(
        span_session,
        row_id=1,
        span_id="es_gap",
        evidence_id="evs_gap",
        quote="significantly\nincreased grain weight was observed.",
    )
    await span_session.flush()

    result = await query_verbatim_evidence(
        span_session,
        tenant_id=1,
        kb_ids=["kb-a"],
        question="原文引述",
        patterns=["significantly increased grain"],
    )
    assert [span["span_id"] for span in result["spans"]] == ["es_gap"]
    assert result["spans"][0]["match_tier"] == "TOKEN_GAP"


@pytest.mark.asyncio
async def test_short_patterns_and_empty_scope_fail_closed(span_session):
    await span_session.flush()
    result = await query_verbatim_evidence(
        span_session, tenant_id=1, kb_ids=["kb-a"], question="ab cd", patterns=["ab"]
    )
    assert result["spans"] == []
    result = await query_verbatim_evidence(
        span_session, tenant_id=None, kb_ids=["kb-a"], question="OsMYB73", patterns=["OsMYB73"]
    )
    assert result["spans"] == []


# ========== gateway 集成层 ==========


@pytest.mark.asyncio
async def test_gateway_verbatim_channel_end_to_end(monkeypatch: pytest.MonkeyPatch):
    async def no_documents(member, query_text):
        return [], None

    async def no_graph(member, query_text, *, limit):
        return [], None

    async def verbatim_source(members, query_text, *, verbatim, limit):
        spans = [
            {
                "span_id": "es_1",
                "evidence_id": "evs_1",
                "anchor_id": "ea_1",
                "page_number": 3,
                "sentence_index": 0,
                "evidence_type": "sentence",
                "container_label": None,
                "row_key": None,
                "quote": "OsMYB73 contains two SANT domains.",
                "kb_id": "kb-a",
                "file_id": "file-a",
                "match_tier": "EXACT_SUBSTRING",
                "matched_value": "OsMYB73",
            }
        ]
        rows = scope_gateway._normalize_verbatim_results(members[0], spans, query_text=query_text)
        return rows, None

    monkeypatch.setattr(scope_gateway, "_query_document_source", no_documents)
    monkeypatch.setattr(scope_gateway, "_query_managed_graph_source", no_graph)
    monkeypatch.setattr(scope_gateway, "_query_verbatim_scope_source", verbatim_source)

    member = {
        "kb_id": "kb-a",
        "kb_name": "Rice PDF",
        "priority": 100,
        "document_enabled": True,
        "graph_enabled": True,
        "structured_enabled": True,
        "evidence_strict": True,
        "evidence_supporting": True,
        "evidence_candidate": False,
        "evidence_rejected": False,
    }
    result = await scope_gateway.query_knowledge_scope_gateway(
        query_text="OsMYB73 的 SANT 结构域",
        scope_snapshot={"members": [member], "effective_kb_ids": ["kb-a"]},
        top_k=6,
        verbatim={"tenant_id": 1, "question_types": ["ENTITY"], "patterns": ["OsMYB73"]},
    )

    assert result["retrieval_summary"]["verbatim_hit_count"] == 1
    assert result["knowledge_source_status"][0]["verbatim_status"] == "AVAILABLE"
    row = result["evidence"][0]
    assert row["retrieval_channel"] == "VERBATIM"
    assert row["claim_eligible"] is False
    assert row["anchor_id"] == "ea_1"
    assert row["page_number"] == 3
    assert row["match_tier"] == "EXACT_SUBSTRING"


@pytest.mark.asyncio
async def test_gateway_without_verbatim_config_keeps_legacy_shape(monkeypatch: pytest.MonkeyPatch):
    async def no_documents(member, query_text):
        return [], None

    async def no_graph(member, query_text, *, limit):
        return [], None

    async def forbidden_verbatim(members, query_text, *, verbatim, limit):
        raise AssertionError("VERBATIM task must not be scheduled without config")

    monkeypatch.setattr(scope_gateway, "_query_document_source", no_documents)
    monkeypatch.setattr(scope_gateway, "_query_managed_graph_source", no_graph)
    monkeypatch.setattr(scope_gateway, "_query_verbatim_scope_source", forbidden_verbatim)

    result = await scope_gateway.query_knowledge_scope_gateway(
        query_text="普通问题",
        scope_snapshot={
            "members": [
                {
                    "kb_id": "kb-a",
                    "kb_name": "Rice PDF",
                    "document_enabled": True,
                    "graph_enabled": True,
                    "structured_enabled": True,
                    "evidence_supporting": True,
                }
            ],
            "effective_kb_ids": ["kb-a"],
        },
        top_k=5,
    )
    assert "verbatim_status" not in result["knowledge_source_status"][0]


def test_merge_verbatim_rows_prefers_document_row_and_inherits_stable_id():
    document = {
        "evidence_id": "evdoc_1",
        "source_type": "DOCUMENT",
        "kb_id": "kb-a",
        "found_in_kbs": ["kb-a"],
        "content": "prefix OsMYB73 regulates grain size. suffix",
        "provenance": [{"kb_id": "kb-a", "source_type": "DOCUMENT", "chunk_id": "c1"}],
        "raw_score": 0.9,
        "priority": 100,
    }
    verbatim = {
        "evidence_id": "evs_stable",
        "source_type": "VERBATIM",
        "retrieval_channel": "VERBATIM",
        "kb_id": "kb-a",
        "found_in_kbs": ["kb-a"],
        "content": "OsMYB73 regulates grain size.",
        "page_number": 3,
        "span_id": "es_1",
        "match_tier": "EXACT_SUBSTRING",
        "provenance": [{"kb_id": "kb-a", "source_type": "VERBATIM", "span_id": "es_1"}],
        "raw_score": 0.76,
        "priority": 100,
    }
    standalone = {**verbatim, "evidence_id": "evs_free", "content": "Not covered by any chunk."}

    merged = scope_gateway._merge_verbatim_rows([document, verbatim, standalone])

    assert [row["source_type"] for row in merged] == ["DOCUMENT", "VERBATIM"]
    assert merged[0]["verbatim_hit"] is True
    assert merged[0]["verbatim_evidence_id"] == "evs_stable"
    assert merged[0]["page_number"] == 3
    assert any(origin["source_type"] == "VERBATIM" for origin in merged[0]["provenance"])
    assert merged[1]["evidence_id"] == "evs_free"


def test_normalize_verbatim_scores_do_not_dominate_semantic_ranking():
    member = {"kb_id": "kb-a", "kb_name": "Rice PDF", "priority": 100}
    span = {
        "span_id": "es_1",
        "evidence_id": "evs_1",
        "anchor_id": "ea_1",
        "page_number": 2,
        "sentence_index": 0,
        "evidence_type": "sentence",
        "container_label": None,
        "row_key": None,
        "quote": "OsMYB73 contains two SANT domains.",
        "kb_id": "kb-a",
        "file_id": "file-a",
        "match_tier": "EXACT_SUBSTRING",
        "matched_value": "OsMYB73",
    }
    rows = scope_gateway._normalize_verbatim_results(member, [span], query_text="OsMYB73 SANT")
    assert len(rows) == 1
    assert rows[0]["raw_score"] == pytest.approx(min(1.0, 1.0 + scope_gateway.VERBATIM_EXACT_BONUS))
    assert rows[0]["claim_eligible"] is False
    assert rows[0]["identifier_status"] == "UNSTRUCTURED_DOCUMENT"


# ========== 输出侧验证器 ==========


def test_validator_flags_untraceable_verbatim_and_accepts_evidence_quote_rows():
    traceable = {
        "evidence_id": "evs_ok",
        "kb_id": "kb-a",
        "retrieval_channel": "VERBATIM",
        "evidence_quote": "OsMYB73 contains two SANT domains.",
        "anchor_id": "ea_1",
        "file_id": "file-a",
    }
    untraceable = {
        "evidence_id": "evs_bad",
        "kb_id": "kb-a",
        "retrieval_channel": "VERBATIM",
        "evidence_quote": "Some verbatim sentence without locator.",
    }
    derived = {
        "evidence_id": "evs_wiki",
        "kb_id": "kb-wiki",
        "retrieval_channel": "DOCUMENT",
        "content": "derived wiki text must fail closed",
    }
    validation, warnings = validate_context_evidence([traceable, untraceable, derived], derived_kb_ids={"kb-wiki"})
    assert validation["status"] == "FAIL"
    assert validation["untraceable_verbatim_ids"] == ["evs_bad"]
    assert validation["empty_content_ids"] == []  # evidence_quote 与 content 任一非空即可
    assert any("VERBATIM" in warning for warning in warnings)

    ok_validation, ok_warnings = validate_context_evidence([traceable], derived_kb_ids=set())
    assert ok_validation["status"] == "PASS"
    assert ok_warnings == []
