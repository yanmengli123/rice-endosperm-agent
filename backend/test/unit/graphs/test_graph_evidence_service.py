"""图谱「点开即见原文」证据服务：显示时校验、定义语句派生、信任分级、完整性报表。"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from yuxi.knowledge.graphs.graph_evidence_service import (
    TRUST_CANDIDATE,
    TRUST_VERIFIED_CORROBORATED,
    TRUST_VERIFIED_SINGLE,
    GraphEvidenceService,
    build_context_snippet,
    build_entity_evidence,
    build_integrity_report,
    build_triple_evidence,
    parse_chunk_provenance,
    verify_quote,
)

_CONTENT = (
    "【文献】10.1111/tpj.12345\n【章节】Results\n"
    "GIF1 encodes a cell-wall invertase. Overexpression of GIF1 increased grain weight in Nipponbare.\n"
    "The gif1 mutant showed reduced grain filling."
)


def _mention(**overrides):
    base = {
        "chunk_id": "c1",
        "file_id": "f1",
        "chunk_index": 0,
        "chunk_content": _CONTENT,
        "source_provenance": None,
        "filename": "liu2024.md",
        "quote": "Overexpression of GIF1 increased grain weight",
        "quote_start_char": None,
    }
    base.update(overrides)
    return base


# ── 显示时校验与上下文 ──────────────────────────────────────────


def test_verify_quote_statuses():
    assert verify_quote("increased grain weight", _CONTENT) == ("OK", _CONTENT.find("increased grain weight"))
    # 仅换行差异（句子在 chunk 里跨行）仍判 OK，但无精确偏移
    assert verify_quote("in Nipponbare. The gif1 mutant", _CONTENT) == ("OK", None)
    assert verify_quote("GIF1 boosts yield", _CONTENT) == ("DEGRADED", None)
    assert verify_quote("", _CONTENT) == ("MISSING", None)


def test_parse_chunk_provenance_prefers_marker_lines_then_source_provenance():
    parsed = parse_chunk_provenance(_CONTENT, {"section_path": ["Intro"], "page_numbers": [3, 4]})
    assert parsed == {"section": "Results", "literature": "10.1111/tpj.12345", "identifiers": None, "page": "3, 4"}

    fallback = parse_chunk_provenance("plain text", {"section_path": ["Results", "3.1"], "page_numbers": []})
    assert fallback["section"] == "Results > 3.1"
    assert fallback["page"] is None


def test_build_context_snippet_strips_marker_lines_and_marks_truncation():
    quote = "Overexpression of GIF1 increased grain weight"
    snippet = build_context_snippet(_CONTENT, quote, None, radius=20)

    assert "【文献】" not in snippet and "【章节】" not in snippet
    assert quote in snippet
    assert snippet.startswith("…") and snippet.endswith("…")
    assert build_context_snippet(_CONTENT, "not there", None) is None


# ── 边证据 ──────────────────────────────────────────────────────


def _triple_source(mentions):
    return {
        "triple": {
            "triple_id": "t1",
            "relation_type": "OVEREXPRESSION_EFFECT",
            "source_entity_id": "e_gif1",
            "target_entity_id": "e_weight",
            "support_count": len(mentions),
            "literature_count": len({m["file_id"] for m in mentions}),
        },
        "source": {"entity_id": "e_gif1", "name": "GIF1", "label": "Gene"},
        "target": {"entity_id": "e_weight", "name": "grain weight", "label": "Phenotype"},
        "mentions": mentions,
    }


def test_build_triple_evidence_ranks_ok_high_confidence_first_and_tiers_trust():
    mentions = [
        _mention(chunk_id="c_bad", quote="GIF1 boosts yield", confidence=0.95, trigger_verified=True),
        _mention(chunk_id="c_hedge", file_id="f2", confidence=0.6, hedge=True, trigger_verified=False),
        _mention(chunk_id="c_best", file_id="f2", confidence=0.9, hedge=False, trigger_verified=True),
    ]

    evidence = build_triple_evidence(_triple_source(mentions))

    assert [m["chunk_id"] for m in evidence["mentions"]] == ["c_best", "c_hedge", "c_bad"]
    assert evidence["mentions"][0]["verification"] == "OK"
    assert evidence["mentions"][0]["section"] == "Results"
    assert evidence["mentions"][0]["literature"] == "10.1111/tpj.12345"
    assert evidence["mentions"][2]["verification"] == "DEGRADED"
    assert evidence["verification_summary"] == {"OK": 2, "DEGRADED": 1, "MISSING": 0}
    assert evidence["literature_count"] == 2
    assert evidence["trust_tier"] == TRUST_VERIFIED_CORROBORATED
    assert evidence["source"] == {"entity_id": "e_gif1", "name": "GIF1", "label": "Gene"}


def test_trust_tier_single_source_and_candidate():
    single = build_triple_evidence(_triple_source([_mention(trigger_verified=True)]))
    assert single["trust_tier"] == TRUST_VERIFIED_SINGLE

    candidate = build_triple_evidence(_triple_source([_mention(file_id="f1"), _mention(chunk_id="c2", file_id="f2")]))
    assert candidate["trust_tier"] == TRUST_CANDIDATE


# ── 节点证据 ────────────────────────────────────────────────────


def test_build_entity_evidence_derives_definition_from_best_relation_chunk():
    source = {
        "entity": {
            "entity_id": "e_gif1",
            "name": "GIF1",
            "label": "Gene",
            "canonical_identity": "name:gif1",
            "attributes": [],
        },
        "aliases": [{"alias": "GRAIN INCOMPLETE FILLING 1", "alias_type": "EXTRACTED"}],
        "mentions": [
            _mention(chunk_id="c_first", quote="GIF1 encodes a cell-wall invertase."),
            _mention(
                chunk_id="c_rel", file_id="f2", quote="Overexpression of GIF1 increased grain weight in Nipponbare."
            ),
            _mention(chunk_id="c_legacy", file_id="f3", quote=None),
        ],
        "relation_mentions": [{"triple_id": "t1", "chunk_id": "c_rel", "confidence": 0.9, "trigger_verified": True}],
        "triple_count": 1,
    }

    evidence = build_entity_evidence(source)

    assert evidence["definition"]["chunk_id"] == "c_rel"
    assert evidence["definition"]["quote"].startswith("Overexpression of GIF1")
    assert evidence["aliases"] == ["GRAIN INCOMPLETE FILLING 1"]
    assert evidence["mention_count"] == 3
    assert evidence["file_count"] == 3
    assert evidence["triple_count"] == 1
    legacy = next(m for m in evidence["mentions"] if m["chunk_id"] == "c_legacy")
    assert legacy["verification"] == "MISSING" and legacy["quote"] is None and legacy["context"] is None
    assert evidence["verification_summary"] == {"OK": 2, "DEGRADED": 0, "MISSING": 1}


def test_build_entity_evidence_falls_back_to_document_order_without_relations():
    source = {
        "entity": {
            "entity_id": "e",
            "name": "GIF1",
            "label": "Gene",
            "canonical_identity": "name:gif1",
            "attributes": [],
        },
        "aliases": [],
        "mentions": [
            _mention(chunk_id="c_bad", quote="not in chunk"),
            _mention(chunk_id="c_ok", quote="GIF1 encodes a cell-wall invertase."),
        ],
        "relation_mentions": [],
        "triple_count": 0,
    }

    evidence = build_entity_evidence(source)

    assert evidence["definition"]["chunk_id"] == "c_ok"


# ── 完整性报表 ──────────────────────────────────────────────────


def test_build_integrity_report_flags_violations():
    ok_report = build_integrity_report(
        {
            "counts": {
                "triples": 2,
                "triples_without_mention": 0,
                "triple_mentions": 2,
                "triple_mentions_without_text": 0,
                "entities": 3,
                "entities_without_mention": 0,
                "entity_mentions": 3,
                "entity_mentions_without_text": 0,
            },
            "quotes": [{"kind": "triple", "chunk_id": "c1", "quote": "increased grain weight", "content": _CONTENT}],
            "limit": 5000,
        }
    )
    assert ok_report["status"] == "OK"
    assert ok_report["checked_quotes"] == {"triple": 1, "entity": 0}

    bad_report = build_integrity_report(
        {
            "counts": {"triples": 1, "triples_without_mention": 1, "entity_mentions_without_text": 2},
            "quotes": [{"kind": "entity", "chunk_id": "c1", "quote": "drifted", "content": _CONTENT}],
            "limit": 5000,
        }
    )
    assert bad_report["status"] == "VIOLATION"
    assert bad_report["violations"]["I1_triples_without_mention"] == 1
    assert bad_report["violations"]["I2_entity_mentions_without_text"] == 2
    assert bad_report["violations"]["I2_entity_quotes_degraded"] == 1


# ── 服务包装与迁移登记 ─────────────────────────────────────────


@pytest.mark.asyncio
async def test_evidence_service_raises_for_missing_elements():
    repo = SimpleNamespace(
        get_triple_evidence_source=AsyncMock(return_value=None),
        get_entity_evidence_source=AsyncMock(return_value=_entity_none()),
    )
    service = GraphEvidenceService(graph_repo=repo)

    with pytest.raises(ValueError, match="不存在"):
        await service.triple_evidence("kb", "missing")
    with pytest.raises(ValueError, match="不存在"):
        await service.entity_evidence("kb", "missing")


def _entity_none():
    return None


def test_migration_0039_is_registered_with_handler():
    from yuxi.storage.postgres.manager import PostgresManager

    versions = [version for version, _ in PostgresManager._VERSIONED_MIGRATIONS]
    assert versions[-1] == "0039_graph_mention_evidence"
    assert callable(getattr(PostgresManager, "_migration_0039_graph_mention_evidence"))
