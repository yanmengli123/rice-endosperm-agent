"""golden 抽检评测（R7b）：晋升门禁的人工标注质量标尺。

对 golden 样本 chunk 用「当前锁定配置」重新抽取（不写图谱），把门禁通过后的
关系与人工标注的期望三元组做集合比对（surface 经同一归一化），输出
precision / recall / F1。纯编排 + 纯比对，抽取器复用构建链同一工厂。
"""

from __future__ import annotations

from typing import Any

from sqlalchemy import select

from yuxi.knowledge.graphs.graph_utils import normalize_entity_name
from yuxi.storage.postgres.manager import pg_manager
from yuxi.storage.postgres.models_knowledge import (
    KnowledgeChunk,
    KnowledgeGraphGoldenSample,
)

GOLDEN_EVALUATION_VERSION = "golden_evaluation_v1"
# 同步 HTTP 评测的样本上限（每样本一次 LLM 批调用，防长事务）
MAX_EVALUATE_SAMPLES = 20


def _triple_key(source: str, predicate: str, obj: str) -> tuple[str, str, str]:
    return (normalize_entity_name(source), str(predicate).strip().upper(), normalize_entity_name(obj))


def compare_triples(predicted: list[dict[str, Any]], expected: list[dict[str, Any]]) -> dict[str, Any]:
    """集合比对（纯函数）：预测/期望各自归一去重后求 P/R/F1 与差集明细。"""
    predicted_keys = {
        _triple_key(item.get("source", ""), item.get("predicate", ""), item.get("object", "")) for item in predicted
    }
    expected_keys = {
        _triple_key(item.get("source", ""), item.get("predicate", ""), item.get("object", "")) for item in expected
    }
    hits = predicted_keys & expected_keys
    precision = len(hits) / len(predicted_keys) if predicted_keys else None
    recall = len(hits) / len(expected_keys) if expected_keys else None
    if precision is not None and recall is not None and (precision + recall) > 0:
        f1 = round(2 * precision * recall / (precision + recall), 4)
    else:
        f1 = 0.0
    return {
        "precision": round(precision, 4) if precision is not None else None,
        "recall": round(recall, 4) if recall is not None else None,
        "f1": f1,
        "hits": len(hits),
        "predicted": len(predicted_keys),
        "expected": len(expected_keys),
        "missing": [list(key) for key in sorted(expected_keys - predicted_keys)][:20],
        "spurious": [list(key) for key in sorted(predicted_keys - expected_keys)][:20],
    }


async def evaluate_golden_samples(
    kb_id: str,
    *,
    extractor: Any,
    limit: int = MAX_EVALUATE_SAMPLES,
) -> dict[str, Any]:
    """对 golden 样本跑当前配置抽取并比对（不写图谱）。extractor 由调用方按锁定配置构建。"""
    async with pg_manager.get_async_session_context() as session:
        sample_rows = (
            await session.execute(
                select(KnowledgeGraphGoldenSample, KnowledgeChunk.content)
                .join(KnowledgeChunk, KnowledgeChunk.chunk_id == KnowledgeGraphGoldenSample.chunk_id)
                .where(KnowledgeGraphGoldenSample.kb_id == kb_id)
                .order_by(KnowledgeGraphGoldenSample.id.asc())
                .limit(max(1, min(limit, MAX_EVALUATE_SAMPLES)))
            )
        ).all()
    per_chunk: list[dict[str, Any]] = []
    total_predicted: list[dict[str, Any]] = []
    total_expected: list[dict[str, Any]] = []
    for sample, content in sample_rows:
        expected = [item for item in (sample.expected_triples or []) if isinstance(item, dict)]
        result = await extractor.extract(content or "", chunk_metadata={"kb_id": kb_id, "chunk_id": sample.chunk_id})
        predicted = [
            {
                "source": relation.get("source", {}).get("text", ""),
                "predicate": relation.get("label", ""),
                "object": relation.get("target", {}).get("text", ""),
            }
            for relation in result.get("relations") or []
        ]
        comparison = compare_triples(predicted, expected)
        per_chunk.append({"chunk_id": sample.chunk_id, **comparison})
        total_predicted.extend(predicted)
        total_expected.extend(expected)
    overall = compare_triples(total_predicted, total_expected) if per_chunk else {}
    return {
        "kb_id": kb_id,
        "evaluation_version": GOLDEN_EVALUATION_VERSION,
        "samples": len(per_chunk),
        "per_chunk": per_chunk,
        "overall": overall,
    }
