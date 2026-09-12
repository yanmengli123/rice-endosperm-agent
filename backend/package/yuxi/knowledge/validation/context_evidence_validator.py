"""通用分支 context_evidence 确定性验证（P2-14）。

枚举/实体分支已有 validate_deterministic_claims + validate_structured_citations；
通用问答分支的证据（文档 chunk / VERBATIM span）此前无验证出口。本验证器只做
确定性检查：

1. evidence_id 唯一（重复=融合/对齐缺陷）；
2. 正文非空（evidence_quote 或 content，空证据不得进入答案上下文）；
3. 来源 kb 非派生产品（Authority Gate 的输出侧复核——上游已拒，输出侧再核）；
4. 问题实体标识符覆盖统计（题型 ENTITY/NUMERIC 的命中报告，供充分性判断）；
5. VERBATIM 行必须携带可回源定位（anchor_id，或 file_id+page_number）。

不判定语义正确性（那是 Faithfulness 语义层的事），失败项给出错误码与整体状态。
"""

from __future__ import annotations

from typing import Any

CONTEXT_EVIDENCE_VALIDATOR_VERSION = "1.1"


def _row_text(item: dict[str, Any]) -> str:
    """gateway 压缩行携带 evidence_quote；结构化行携带 content——两者任一非空即可。"""
    return str(item.get("evidence_quote") or item.get("content") or "").strip()


def validate_context_evidence(
    context_evidence: list[dict[str, Any]],
    *,
    derived_kb_ids: set[str] | None = None,
    required_identifiers: list[str] | None = None,
) -> tuple[dict[str, Any], list[str]]:
    """返回 (validation DTO, warnings)；status ∈ PASS/DEGRADED/FAIL。"""
    derived_kb_ids = derived_kb_ids or set()
    warnings: list[str] = []
    seen_ids: set[str] = set()
    invalid: list[str] = []
    empty_content: list[str] = []
    derived_hits: list[str] = []
    untraceable_verbatim: list[str] = []

    for item in context_evidence:
        evidence_id = str(item.get("evidence_id") or "")
        if not evidence_id:
            invalid.append(f"#{len(invalid) + 1}")
            continue
        if evidence_id in seen_ids:
            invalid.append(evidence_id)
            warnings.append(f"证据 {evidence_id} 重复出现，已按首次出现校验")
            continue
        seen_ids.add(evidence_id)
        if not _row_text(item):
            empty_content.append(evidence_id)
        kb_id = str(item.get("kb_id") or "")
        if kb_id and kb_id in derived_kb_ids:
            derived_hits.append(evidence_id)
        if item.get("retrieval_channel") == "VERBATIM" and not (
            item.get("anchor_id") or (item.get("file_id") and item.get("page_number"))
        ):
            untraceable_verbatim.append(evidence_id)

    if untraceable_verbatim:
        warnings.append(f"以下 VERBATIM 证据缺少可回源定位（anchor 或 文件+页码）：{', '.join(untraceable_verbatim)}")

    identifiers = [str(value) for value in (required_identifiers or []) if str(value).strip()]
    identifier_coverage = 0.0
    if identifiers:
        corpus = " ".join(_row_text(item) for item in context_evidence).casefold()
        hits = sum(1 for identifier in identifiers if identifier.casefold() in corpus)
        identifier_coverage = round(hits / len(identifiers), 4)
        if hits < len(identifiers):
            missing = [identifier for identifier in identifiers if identifier.casefold() not in corpus]
            warnings.append(f"以下问题标识符未在证据中出现：{', '.join(missing)}")

    if invalid or empty_content or derived_hits or untraceable_verbatim:
        status = "FAIL" if derived_hits else "DEGRADED"
    else:
        status = "PASS"
    validation = {
        "validator": "context_evidence_v1",
        "version": CONTEXT_EVIDENCE_VALIDATOR_VERSION,
        "status": status,
        "total": len(context_evidence),
        "invalid_ids": invalid,
        "empty_content_ids": empty_content,
        "derived_source_ids": derived_hits,
        "untraceable_verbatim_ids": untraceable_verbatim,
        "identifier_coverage": identifier_coverage,
    }
    return validation, warnings
