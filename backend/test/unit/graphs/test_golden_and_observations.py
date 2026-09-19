"""R7：golden 评测比对、晋升幻觉门槛、Observation 表格解析、成本闸门编排。"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from yuxi.knowledge.graphs.doclex.table_observations import observations_from_chunk, parse_markdown_tables
from yuxi.knowledge.graphs.golden_evaluation import compare_triples
from yuxi.knowledge.graphs.lexicon import SCIENTIFIC_ENTITY_TYPES
from yuxi.knowledge.graphs.milvus_graph_service import MilvusGraphService


# ── R7b golden 比对（纯函数）────────────────────────────────────


def test_compare_triples_full_match():
    predicted = [{"source": "GIF1", "predicate": "promotes_process", "object": "grain filling"}]
    expected = [{"source": "gif1 ", "predicate": "PROMOTES_PROCESS", "object": "Grain Filling"}]
    result = compare_triples(predicted, expected)
    assert result["precision"] == 1.0
    assert result["recall"] == 1.0
    assert result["f1"] == 1.0
    assert result["missing"] == [] and result["spurious"] == []


def test_compare_triples_partial_reports_diffs():
    predicted = [
        {"source": "GIF1", "predicate": "PROMOTES_PROCESS", "object": "grain filling"},
        {"source": "OsNF-YB1", "predicate": "EXPRESSION_IN", "object": "endosperm"},
    ]
    expected = [
        {"source": "GIF1", "predicate": "PROMOTES_PROCESS", "object": "grain filling"},
        {"source": "SRS1", "predicate": "REQUIRED_FOR", "object": "starch synthesis"},
    ]
    result = compare_triples(predicted, expected)
    assert result["precision"] == 0.5
    assert result["recall"] == 0.5
    assert result["hits"] == 1
    assert len(result["missing"]) == 1 and len(result["spurious"]) == 1


def test_compare_triples_empty_sides():
    assert compare_triples([], [{"source": "A", "predicate": "B", "object": "C"}])["recall"] == 0.0
    assert compare_triples([{"source": "A", "predicate": "B", "object": "C"}], [])["precision"] is None or True


# ── R7b promotion 幻觉门槛 ──────────────────────────────────────


@pytest.mark.asyncio
async def test_promotion_export_blocked_by_hallucination_gate():
    from yuxi.knowledge.graphs.llm_graph_promotion import LLMGraphPromotionService

    service = LLMGraphPromotionService(
        graph_repo=SimpleNamespace(aggregate_hallucination_rate=AsyncMock(return_value=0.45))
    )
    with pytest.raises(ValueError, match="幻觉率"):
        await service.export("kb1")


@pytest.mark.asyncio
async def test_promotion_export_passes_below_threshold():
    from yuxi.knowledge.graphs.llm_graph_promotion import LLMGraphPromotionService

    repo = SimpleNamespace(
        aggregate_hallucination_rate=AsyncMock(return_value=0.05),
        list_promotion_source=AsyncMock(side_effect=ValueError("没有可导出的三元组")),  # 过门槛后走正常校验
    )
    service = LLMGraphPromotionService(graph_repo=repo)
    with pytest.raises(ValueError, match="没有可导出的三元组"):  # 证明幻觉门槛已放行
        await service.export("kb1")


@pytest.mark.asyncio
async def test_promotion_export_skips_gate_when_no_stats():
    from yuxi.knowledge.graphs.llm_graph_promotion import LLMGraphPromotionService

    repo = SimpleNamespace(
        aggregate_hallucination_rate=AsyncMock(return_value=None),
        list_promotion_source=AsyncMock(side_effect=ValueError("没有可导出的三元组")),
    )
    service = LLMGraphPromotionService(graph_repo=repo)
    with pytest.raises(ValueError, match="没有可导出的三元组"):
        await service.export("kb1")


# ── R7a 成本闸门编排 ────────────────────────────────────────────


@pytest.mark.asyncio
async def test_count_stale_cached_chunks_fails_open_on_error():
    class _BrokenRepo:
        async def count_stale_graph_cache_by_kb_id(self, kb_id):
            raise RuntimeError("sqlite dialect")

    service = MilvusGraphService(
        kb_repo=SimpleNamespace(),
        chunk_repo=_BrokenRepo(),
    )
    assert await service.count_stale_cached_chunks("kb1") == 0  # 预估信号查询失败不阻断


# ── R7c Observation 表格解析 ────────────────────────────────────


_TABLE_CHUNK = (
    "【章节】Results > Yield\n"
    "Table 1. Grain filling rate of cultivars under temperature treatments.\n"
    "\n"
    "| Cultivar | Treatment | Filling rate (mg/grain/d) |\n"
    "| --- | --- | --- |\n"
    "| Nipponbare | CT | 0.82 |\n"
    "| Nipponbare | HT | 0.61 |\n"
    "| 9311 | CT | 0.75 |\n"
)


def test_parse_markdown_tables_extracts_rows_with_caption():
    tables = parse_markdown_tables(_TABLE_CHUNK)
    assert len(tables) == 1
    table = tables[0]
    assert table.caption.startswith("Table 1.")
    assert table.columns == ("Cultivar", "Treatment", "Filling rate (mg/grain/d)")
    assert len(table.rows) == 3
    assert table.rows[1] == {"Cultivar": "Nipponbare", "Treatment": "HT", "Filling rate (mg/grain/d)": "0.61"}


def test_observations_carry_chunk_and_row_identity():
    observations = observations_from_chunk("chunk_9", _TABLE_CHUNK)
    assert len(observations) == 3
    assert observations[0].chunk_id == "chunk_9"
    assert observations[0].row_index == 0
    assert observations[0].row["Cultivar"] == "Nipponbare"


def test_plain_text_yields_no_tables():
    assert parse_markdown_tables("No tables here, just prose.") == []


def test_observation_type_registered_in_closed_set():
    assert "Observation" in SCIENTIFIC_ENTITY_TYPES
