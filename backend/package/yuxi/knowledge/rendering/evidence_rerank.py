"""文献专属会话·检索证据后置重排（批次 4）。

仅对 paper 会话生效（scope_snapshot 携带 literature_binding），默认关闭
（``literature_rerank_enabled``）。企业级三治理红线（照抄已验证的
``literature/search_runtime.py:178-253`` 范式）：

1. **严格校验**：rerank 返回分数数量必须与输入 evidence 一一对应、且均为有限
   浮点数，否则 raise → 回退原序；
2. **拒绝静默 0.5**：绕开 ``models/rerank.py`` 的 ``acompute_score``（其 except
   分支会把失败批次静默填 0.5 分，正是幻觉隐患），直接用底层 ``_batch_rerank``
   （异常传播）；
3. **治理边界**：服务层注入 scorer，负责冻结凭据、配额、账本及调用前后权限复验。
   本模块仅验证排序集合不变；PermissionError 必须传播，不能作为供应商错误回退。

挂点约束：必须在 ``_gateway_contract`` 与 citation 构建**之前**重排
``result["evidence"]``，使 claim 内顺序、citation 芯片 E# 编号、模型可见顺序
（``build_answer_context`` 前 10 条）三者一致，血统不错位。

供应商失败/闸门关闭/数据不足保留原序；权限或版本变化必须拒绝继续使用证据。
"""

from __future__ import annotations

import math
import time
from collections.abc import Awaitable, Callable
from typing import Any

from yuxi.trace.recorder import emit_trace
from yuxi.utils.logging_config import logger


def _evidence_text(row: dict[str, Any]) -> str:
    """取 evidence 行的可重排文本（content 优先，回退 quote 类字段）。"""
    for key in ("content", "evidence_quote", "quote", "text"):
        value = row.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return ""


async def rerank_literature_evidence(
    result: dict[str, Any],
    *,
    question: str,
    scope_snapshot: dict[str, Any],
    scorer: Callable[[str, list[dict[str, Any]]], Awaitable[list[float]]] | None = None,
) -> dict[str, Any]:
    """对 paper 会话检索证据做后置重排（in-place 重排 ``result["evidence"]``）。

    返回重排元数据；闸门未命中时不发 trace、不改 contract（默认行为逐字节不变）。
    """
    # Network/credentials/ledger live in the service-owned scorer, not this rendering layer.
    options = scope_snapshot.get("literature_enhancement") or {}
    if not (scope_snapshot.get("literature_binding") and options.get("rerank_enabled")):
        return {"applied": False, "reason": "gate_closed"}

    evidence = [row for row in result.get("evidence") or [] if isinstance(row, dict)]
    if len(evidence) < 2:
        return {"applied": False, "reason": "insufficient_evidence"}
    if len(evidence) > 48:
        return {"applied": False, "reason": "candidate_budget_exceeded"}
    if scorer is None:
        return {"applied": False, "reason": "governed_scorer_unavailable"}
    original_ids = [str(row.get("evidence_id") or "") for row in evidence]
    if len(evidence) != len(result["evidence"]) or not all(original_ids) or len(set(original_ids)) != len(original_ids):
        return {"applied": False, "reason": "invalid_evidence_identity"}

    documents = [_evidence_text(row) for row in evidence]
    # 全部无文本可重排 → 原序
    if not any(documents):
        return {"applied": False, "reason": "no_rerankable_text"}

    started = time.monotonic()
    applied = False
    reason: str | None = None
    try:
        scores = await scorer(question, evidence)
        # 红线 1（续）：数量 + 有限浮点校验（对齐 search_runtime.py:240）
        if len(scores) != len(documents) or not all(math.isfinite(score) for score in scores):
            raise ValueError("重排结果数量或数值无效")
        # 稳定降序：同分保持原序（Python sort 稳定）
        order = sorted(range(len(evidence)), key=lambda index: scores[index], reverse=True)
        reranked = [evidence[index] for index in order]
        # Identity invariant only; actual reauthorization is performed by the governed scorer.
        reranked_ids = sorted(str(row.get("evidence_id")) for row in reranked)
        if sorted(original_ids) != reranked_ids:
            raise ValueError("rerank identity mismatch (evidence_id set changed)")
        result["evidence"] = reranked
        applied = True
    except PermissionError:
        raise
    except Exception as exc:  # noqa: BLE001 - only optional ranking failures may degrade
        reason = f"rerank_failed:{type(exc).__name__}"
        logger.warning(f"literature rerank fallback to original order ({reason})")

    _emit(applied, reason, len(evidence), started)
    return {"applied": applied, "reason": reason, "evidence_count": len(evidence)}


def _emit(applied: bool, reason: str | None, evidence_count: int, started: float) -> None:
    """Observability only; the service-owned scorer settles append-only usage."""
    duration_ms = int((time.monotonic() - started) * 1000)
    emit_trace(
        category="ANSWER",
        operation="literature_evidence_reranked",
        event_type="answer.literature_evidence_reranked",
        applied=applied,
        evidence_count=evidence_count,
        duration_ms=duration_ms,
        reason=(reason or "")[:64],
    )
