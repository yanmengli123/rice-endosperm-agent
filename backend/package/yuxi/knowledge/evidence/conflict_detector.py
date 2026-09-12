"""证据矛盾检测器（P3）。

确定性规则、无模型参与。在一条 run 的已验证证据集合上检测两类矛盾：

1. **数值区间冲突**（NUMERIC_CONFLICT）：同一标识符 + 同一量纲词元的
   数值区间互不相交（如 "115-164 aa" vs "167-215 aa" 出现在同一标识符的
   两条证据里且都自称是该标识符的同一属性——保守起见，只对「同文件不同页」
   或「不同文件」的引用报冲突，同页并行不算）；
2. **方向性冲突**（DIRECTION_CONFLICT）：同一 (主体标识符) 的证据引用中出现
   互斥方向词（上调/下调、increase/decrease、promote/inhibit）。

输出冲突对（evidence_id 对 + 冲突类型 + 说明）。只提示，不删除任何证据；
前端以「存在矛盾提示」徽标展示，最终裁决权在用户。
"""

from __future__ import annotations

import re
from typing import Any

CONFLICT_NUMERIC = "NUMERIC_CONFLICT"
CONFLICT_DIRECTION = "DIRECTION_CONFLICT"
CONFLICT_DETECTOR_VERSION = "1.0"

MAX_CONFLICTS_PER_RUN = 20

_IDENTIFIER_FOR_CONFLICT = re.compile(r"(?<![A-Za-z])(?:Os[A-Z][A-Za-z0-9]+|LOC_Os\w+|Os\d+g\w+)\b")
_NUMERIC_RANGE = re.compile(
    r"(\d+(?:[.,]\d+)?)\s*[-–~]\s*(\d+(?:[.,]\d+)?)\s*(%|％|倍|mg/L|mM|µM|μM|kb|bp|aa|氨基酸|cM)?",
    re.IGNORECASE,
)
_UP_WORDS = re.compile(
    r"(?:上调|增加|促进|增强|升高|up-?regulat\w*|increas\w*|promot\w*|enhanc\w*)",
    re.IGNORECASE,
)
_DOWN_WORDS = re.compile(
    r"(?:下调|减少|抑制|降低|down-?regulat\w*|decreas\w*|inhibit\w*|reduc\w*|suppress\w*)",
    re.IGNORECASE,
)


def _parse_range(text: str) -> tuple[float, float, str] | None:
    match = _NUMERIC_RANGE.search(text)
    if not match:
        return None
    try:
        low = float(match.group(1).replace(",", "."))
        high = float(match.group(2).replace(",", "."))
    except ValueError:
        return None
    if high < low:
        low, high = high, low
    return low, high, (match.group(3) or "").lower()


def _ranges_conflict(first: tuple[float, float, str], second: tuple[float, float, str]) -> bool:
    """同量纲且区间不相交才算冲突；量纲不同（aa vs kb）不比较。"""
    if not first[2] or not second[2]:
        return False
    if first[2] != second[2]:
        return False
    return first[1] < second[0] or second[1] < first[0]


def detect_evidence_conflicts(evidence_items: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """在已出境证据（OK/DEGRADED）上检测矛盾；输入为证据 DTO 列表。"""
    conflicts: list[dict[str, Any]] = []
    citable = [item for item in evidence_items if item.get("citable")]

    # 1) 数值区间冲突：同一标识符的不同证据引用不相交区间
    by_identifier: dict[str, list[tuple[dict[str, Any], tuple[float, float, str]]]] = {}
    for item in citable:
        quote = str((item.get("quote") or {}).get("exact") or "")
        identifiers = _IDENTIFIER_FOR_CONFLICT.findall(quote)
        span = _parse_range(quote)
        if not identifiers or span is None:
            continue
        for identifier in identifiers:
            by_identifier.setdefault(identifier, []).append((item, span))

    seen_pairs: set[tuple[str, str]] = set()
    for identifier, entries in by_identifier.items():
        for index in range(len(entries)):
            for other in range(index + 1, len(entries)):
                item_a, span_a = entries[index]
                item_b, span_b = entries[other]
                id_a = str(item_a.get("evidence_id") or "")
                id_b = str(item_b.get("evidence_id") or "")
                if id_a == id_b or not _ranges_conflict(span_a, span_b):
                    continue
                pair = tuple(sorted((id_a, id_b)))
                if pair in seen_pairs:
                    continue
                seen_pairs.add(pair)
                conflicts.append(
                    {
                        "conflict_type": CONFLICT_NUMERIC,
                        "identifier": identifier,
                        "evidence_ids": [id_a, id_b],
                        "detail": (
                            f"{identifier} 的区间 [{span_a[0]}-{span_a[1]} {span_a[2]}] 与 "
                            f"[{span_b[0]}-{span_b[1]} {span_b[2]}] 不相交，请核对原文"
                        ),
                    }
                )
                if len(conflicts) >= MAX_CONFLICTS_PER_RUN:
                    return conflicts

    # 2) 方向性冲突：同一标识符同时被上调与下调描述
    directions: dict[str, dict[str, list[str]]] = {}
    for item in citable:
        quote = str((item.get("quote") or {}).get("exact") or "")
        for identifier in _IDENTIFIER_FOR_CONFLICT.findall(quote):
            has_up = bool(_UP_WORDS.search(quote))
            has_down = bool(_DOWN_WORDS.search(quote))
            if has_up == has_down:
                continue
            direction = "up" if has_up else "down"
            bucket = directions.setdefault(identifier, {"up": [], "down": []})
            evidence_id = str(item.get("evidence_id") or "")
            if evidence_id not in bucket[direction]:
                bucket[direction].append(evidence_id)
    for identifier, bucket in directions.items():
        if bucket["up"] and bucket["down"]:
            conflicts.append(
                {
                    "conflict_type": CONFLICT_DIRECTION,
                    "identifier": identifier,
                    "evidence_ids": [bucket["up"][0], bucket["down"][0]],
                    "detail": (
                        f"{identifier} 同时被上调与下调描述（{bucket['up'][0]} vs {bucket['down'][0]}），"
                        "可能来自不同实验条件，请核对原文语境"
                    ),
                }
            )
            if len(conflicts) >= MAX_CONFLICTS_PER_RUN:
                break
    return conflicts
